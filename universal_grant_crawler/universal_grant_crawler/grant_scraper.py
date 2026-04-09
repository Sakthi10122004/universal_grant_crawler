"""
grant_scraper.py — Production-grade AI grant scraper (Full Crawler)
=====================================================================
Architecture:
  1. Site Analyser → detects page type, counts total pages/grants, asks user
  2. Playwright    → renders JS-heavy pages in headless Chromium
  3. Pagination    → follows Next buttons up to user-chosen page count
  4. Infinite scroll / Load-more → scrolls/clicks until user-chosen item count
  5. Groq API      → primary LLM (llama3-70b, free: 14,400 req/day)
  6. Gemini API    → fallback LLM (gemini-flash, free: 1,500 req/day)
  7. Queue file    → pages exceeding daily limits, retried next run

Usage:
  python grant_scraper.py https://example.com/grants
  python grant_scraper.py https://example.com/grants --output grants.json --append
  python grant_scraper.py https://example.com/grants --wait 8
  python grant_scraper.py --status
  python grant_scraper.py --retry-queue
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from . import config
from .rate_tracker import RateLimitTracker
from .site_analyser import analyse_site, prompt_user, ScrapeConfig

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Run: pip install playwright && playwright install chromium")
    sys.exit(1)

QUEUE_FILE = Path("retry_queue.json")

EXTRACT_PROMPT = """You are a world-class grant data extraction AI. Your output must be 100% accurate.

Extract EVERY distinct grant listed on this page into a valid JSON array.
Output ONLY the raw JSON array. No markdown, no explanation, no preamble. Start with [ and end with ].

Each grant object must have EXACTLY these 8 keys:
{{
  "title":             "...",
  "funding_amount":    "...",
  "thematic_area":     "...",
  "deadline":          "...",
  "country":           "...",
  "organization":      "...",
  "source_url":        "...",
  "short_description": "..."
}}

━━━ FIELD-BY-FIELD RULES ━━━

title:
  Use the EXACT official name. Copy it verbatim. Never truncate.

funding_amount:
  Extract the EXACT monetary value stated on the page.
  Always include the currency symbol: $, EUR, INR, GBP, etc.
  Spell out: "$2 million" NOT "$2M", "Rs. 50 lakh" NOT "50L".
  Range: "$10,000 - $50,000". Cap: "Up to $50,000".
  NEVER guess. If no amount is written anywhere: "Not specified".

thematic_area:
  List specific focus areas separated by commas.
  e.g. "Climate Change, Renewable Energy" or "Women Empowerment, Health".
  Never write just "Development" or "Social" — be specific.

deadline:
  Convert to YYYY-MM-DD. Rolling deadlines: "Rolling". Year-only: "YYYY-12-31".
  NEVER guess. If not mentioned: "Not specified".

country:
  This is WHERE applicants must be FROM or WHERE the project must take place.
  Look for: "open to", "eligible countries", "for organizations in", "applicants from".
  If international/global: "Global". List if multiple: "India, Kenya, Bangladesh".
  If implied by the funder location, use that country.
  NEVER use "Not specified" if it can be reasonably inferred.

organization:
  The entity OFFERING the money (funder, donor, foundation, government body).
  Not the applicant. Extract exactly as written.

source_url:
  Find the [URL: https://...] annotation closest to this grant in the text.
  Use the MOST SPECIFIC deep link for this individual grant's detail page.
  If no deep link exists, use exactly: {url}

short_description:
  Write 2-3 original sentences (max 80 words):
  1. What the grant funds
  2. Who is eligible
  3. Any key requirement or theme
  Do NOT copy-paste. Paraphrase in clear professional English.

━━━ OUTPUT RULES ━━━
- One JSON object per grant. If 10 grants are listed, return 10 objects.
- Every object must have ALL 8 keys.
- If a value truly cannot be found: "Not specified"
- Return ONLY the JSON array. Nothing before [, nothing after ].

PAGE URL: {url}

PAGE CONTENT:
{content}
"""

REQUIRED_FIELDS = [
    "title", "funding_amount", "thematic_area", "deadline",
    "country", "organization", "short_description",
]

NEXT_PAGE_SELECTORS = [
    ("next-rel",   'a[rel="next"]'),
    ("next-text",  'a:has-text("Next")'),
    ("next-text",  'a:has-text("next")'),
    ("next-btn",   'button:has-text("Next")'),
    ("next-class", '.next a'),
    ("next-class", '.pagination-next a'),
    ("next-class", '.pagination__next'),
    ("next-aria",  '[aria-label="Next page"]'),
    ("next-aria",  '[aria-label="next"]'),
]

LOAD_MORE_SELECTORS = [
    'button:has-text("Load More")',
    'button:has-text("Load more")',
    'button:has-text("Show More")',
    'button:has-text("Show more")',
    'button:has-text("View More")',
    '[class*="load-more"]',
    '[class*="loadmore"]',
    '[id*="load-more"]',
]


# =============================================================================
# PAGE HELPERS
# =============================================================================

def extract_text_from_page(page) -> str:
    text = page.evaluate("""() => {
        const hiddenEls = [];
        const selectors = ['script','style','noscript','nav','footer','header',
             'iframe','svg','aside','form', '[class*="cookie"]','[class*="banner"]',
             '[class*="popup"]','[id*="cookie"]'];
        selectors.forEach(sel => {
            try {
                document.querySelectorAll(sel).forEach(el => {
                    if (el.style.display !== 'none') {
                        hiddenEls.push({el: el, oldDisplay: el.style.display});
                        el.style.display = 'none';
                    }
                });
            } catch(e) {}
        });
        
        // Expose HREFS in the text for the LLM
        const modifiedLinks = [];
        document.querySelectorAll('a[href]').forEach(a => {
            if(a.offsetParent !== null && !a.innerText.includes('http') && a.href.startsWith('http')) {
                modifiedLinks.push({el: a, oldText: a.innerText});
                a.innerText = a.innerText + ' [URL: ' + a.href + ']';
            }
        });

        const txt = document.body.innerText;

        // Restore DOM to prevent SPA breakage
        modifiedLinks.forEach(item => {
            item.el.innerText = item.oldText;
        });
        hiddenEls.forEach(item => {
            item.el.style.display = item.oldDisplay;
        });
        return txt;
    }""")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def handle_infinite_scroll(page, wait_seconds: int, max_items: int, items_per_page: int) -> None:
    """Scrolls until we have enough items or no new content loads."""
    scroll_pause     = max(1500, wait_seconds * 500)
    max_scrolls      = config.MAX_SCROLL_ATTEMPTS
    prev_height      = -1
    no_change_streak = 0
    scrolls          = 0

    print("  📜 Infinite scroll: loading content...")

    while scrolls < max_scrolls:
        # Stop early if we've loaded enough items
        if max_items and items_per_page:
            pages_needed = (max_items + items_per_page - 1) // items_per_page
            if scrolls >= pages_needed:
                print(f"  📜 Loaded enough content for {max_items} items.")
                break

        curr_height = page.evaluate("() => document.body.scrollHeight")
        if curr_height == prev_height:
            no_change_streak += 1
            if no_change_streak >= 3:
                print(f"  📜 No new content — done scrolling.")
                break
        else:
            no_change_streak = 0

        prev_height = curr_height
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(scroll_pause)
        scrolls += 1

    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def handle_load_more(page, wait_seconds: int, max_clicks: int = None) -> bool:
    """Clicks load-more buttons up to max_clicks times."""
    found_any   = False
    click_pause = max(2000, wait_seconds * 1000)
    limit       = max_clicks or config.MAX_LOAD_MORE_CLICKS

    for i in range(limit):
        clicked = False
        for sel in LOAD_MORE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=800) and btn.is_enabled():
                    print(f"  🔘 Load more #{i+1}...")
                    btn.scroll_into_view_if_needed()
                    page.wait_for_timeout(400)
                    btn.click()
                    page.wait_for_timeout(click_pause)
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except PWTimeout:
                        pass
                    found_any = True
                    clicked   = True
                    break
            except Exception:
                continue
        if not clicked:
            break

    if found_any:
        print("  ✔  Load-more done.")
    return found_any


def advance_to_next_page(page, wait_seconds: int) -> bool:
    """Clicks the next page button. Returns True if successful, False if no button."""
    for label, selector in NEXT_PAGE_SELECTORS:
        try:
            el = page.locator(selector).first
            if el.is_visible(timeout=500):
                print(f"  ➡  Next page [{label}] clicked.")
                el.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                el.click(timeout=3000)
                
                page.wait_for_timeout(wait_seconds * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except PWTimeout:
                    pass
                return True
        except Exception:
            continue

    # Numbered fallback
    try:
        clicked = page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href], button'));
            const active = document.querySelector('a.active, button.active, [aria-current="page"]');
            if (!active) return false;
            const num = parseInt(active.innerText.trim());
            if (isNaN(num)) return false;
            
            const next = links.find(el => parseInt(el.innerText.trim()) === num + 1);
            if (next) {
                next.click();
                return true;
            }
            return false;
        }""")
        if clicked:
            print(f"  ➡  Next page [numbered] clicked.")
            page.wait_for_timeout(wait_seconds * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except PWTimeout:
                pass
            return True
    except Exception:
        pass

    return False


# =============================================================================
# FULL CRAWLER — respects ScrapeConfig limits
# =============================================================================

def fetch_all_pages(start_url: str, cfg: ScrapeConfig,
                    wait_seconds: int) -> list:
    """
    Crawls pages respecting user-chosen max_pages and max_items.
    Returns list of (text, title, url).
    """
    results     = []
    visited     = set()
    current_url = start_url
    page_num    = 1
    total_items_seen = 0

    # Reuse first page text from analyser (already loaded)
    first_page_text  = cfg.site_info.first_page_text
    first_page_title = cfg.site_info.first_page_title

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        last_text = None

        while current_url and page_num <= cfg.max_pages:
            print(f"\n  🌐 Page {page_num}/{cfg.max_pages}: {current_url}")

            if page_num == 1:
                try:
                    page.goto(current_url, wait_until="networkidle",
                              timeout=config.REQUEST_TIMEOUT * 1000)
                except PWTimeout:
                    print("  ⚠  Timed out — partial content.")
                page.wait_for_timeout(wait_seconds * 1000)
                title = first_page_title
            else:
                title = page.title()

            if cfg.enable_scroll:
                handle_infinite_scroll(page, wait_seconds,
                                       cfg.max_items, cfg.site_info.items_per_page)
                handle_load_more(page, wait_seconds)
            else:
                handle_load_more(page, wait_seconds,
                                 max_clicks=_load_more_clicks_needed(cfg))

            text = extract_text_from_page(page)

            if text == last_text:
                print("  ⚠  Content didn't change — empty or SPA end of pagination.")
                break
            last_text = text

            results.append((text, title, current_url))
            print(f"  ✔  Captured: '{title}' ({len(text):,} chars)")

            # Check item limit
            if cfg.max_items and cfg.site_info.items_per_page:
                total_items_seen += cfg.site_info.items_per_page
                if total_items_seen >= cfg.max_items:
                    print(f"  🛑 Item limit reached ({cfg.max_items}).")
                    break

            # Next page
            if page_num < cfg.max_pages and cfg.site_info.site_type == "paginated":
                success = advance_to_next_page(page, wait_seconds)
                if not success:
                    print(f"  🛑 Could not find next page link.")
                    break
            elif page_num >= cfg.max_pages:
                print(f"  🛑 Page limit reached ({cfg.max_pages}).")

            page_num += 1
            current_url = page.url

        browser.close()

    print(f"\n  📄 Pages crawled: {len(results)}")
    return results


def _load_more_clicks_needed(cfg: ScrapeConfig) -> int:
    """Estimate how many Load More clicks are needed to reach max_items."""
    if cfg.max_items and cfg.site_info.items_per_page:
        return max(1, (cfg.max_items // cfg.site_info.items_per_page) - 1)
    return config.MAX_LOAD_MORE_CLICKS


# =============================================================================
# LLM EXTRACTORS
# =============================================================================

def _parse_llm_response(raw: str, url: str) -> list:
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    grants = json.loads(raw)
    if isinstance(grants, dict):
        grants = [grants]
    for g in grants:
        # Only fallback to base URL if the LLM didn't extract a unique deep link
        if not g.get("source_url") or g.get("source_url") == "Not specified" or g.get("source_url") == url:
            g["source_url"] = url
        for f in REQUIRED_FIELDS:
            if f not in g or not g[f]:
                g[f] = "Not specified"
    return grants


def extract_with_provider(text: str, url: str, p: dict) -> list:
    name = p["provider_name"].lower()
    prompt = EXTRACT_PROMPT.format(url=url, content=text[:config.MAX_CONTENT_CHARS])
    
    if name == "groq":
        from groq import Groq
        client = Groq(api_key=p["api_key"])
        print(f"  🤖 Groq ({p['model_name']}): extracting...")
        resp = client.chat.completions.create(
            model=p["model_name"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8192, temperature=0.1,
        )
        return _parse_llm_response(resp.choices[0].message.content, url)
        
    elif name == "gemini":
        from google import genai
        client = genai.Client(api_key=p["api_key"])
        print(f"  🤖 Gemini ({p['model_name']}): extracting...")
        resp = client.models.generate_content(model=p["model_name"], contents=prompt)
        return _parse_llm_response(resp.text, url)
        
    elif name == "openai" or name == "anthropic":
        from openai import OpenAI
        # Assuming open-ai compatible endpoints for simplicity, or openai directly
        client = OpenAI(api_key=p["api_key"])
        print(f"  🤖 OpenAI/Comp ({p['model_name']}): extracting...")
        resp = client.chat.completions.create(
            model=p["model_name"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096, temperature=0.1,
        )
        return _parse_llm_response(resp.choices[0].message.content, url)
        
    elif name == "ollama":
        import requests
        print(f"  🤖 Ollama ({p['model_name']}): extracting locally...")
        resp = requests.post("http://localhost:11434/api/chat", json={
            "model": p["model_name"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 4096}
        })
        return _parse_llm_response(resp.json()["message"]["content"], url)
        
    return []


def extract_grants(page_text: str, url: str, tracker: RateLimitTracker, providers: list) -> tuple:
    for attempt in range(1, config.RETRY_ATTEMPTS + 1):
        if attempt > 1:
            print(f"  🔁 Retry {attempt}/{config.RETRY_ATTEMPTS}...")
            time.sleep(config.RETRY_DELAY_SECONDS)

        for p in providers:
            prov_name = p["provider_name"]
            if tracker.can_use(prov_name):
                try:
                    grants = extract_with_provider(page_text, url, p)
                    if grants:
                        tracker.increment(prov_name)
                        print(f"  ✔  {prov_name}: {len(grants)} grant(s).")
                        return grants, prov_name
                except Exception as e:
                    print(f"  ⚠  {prov_name} Error: {e}")
            else:
                print(f"  ⚡ {prov_name} limit reached.")

    return [], "none"


# =============================================================================
# DEDUPLICATION
# =============================================================================

def deduplicate_grants(grants: list, max_items: int = 0) -> list:
    seen, unique = set(), []
    for g in grants:
        key = g.get("title", "").lower().strip()
        if key and key != "not specified" and key not in seen:
            seen.add(key)
            unique.append(g)
            if max_items and len(unique) >= max_items:
                print(f"  🛑 Reached requested item limit ({max_items}).")
                break
    removed = len(grants) - len(unique)
    if removed:
        print(f"  🔁 Removed {removed} duplicate(s).")
    return unique


# =============================================================================
# QUEUE
# =============================================================================

def queue_url(url: str, output: str, append: bool) -> None:
    queue = []
    if QUEUE_FILE.exists():
        try:
            queue = json.loads(QUEUE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    if not any(item["url"] == url for item in queue):
        queue.append({"url": url, "output": output, "append": append,
                      "queued_at": datetime.now().isoformat()})
        QUEUE_FILE.write_text(json.dumps(queue, indent=2))
        print(f"  📋 Queued for next run → {QUEUE_FILE}")


def process_queue(tracker: RateLimitTracker, output: str, append: bool, wait: int) -> None:
    if not QUEUE_FILE.exists():
        print("  ℹ  No retry queue found.")
        return
    try:
        queue = json.loads(QUEUE_FILE.read_text())
    except json.JSONDecodeError:
        print("  ⚠  Queue corrupted.")
        return
    if not queue:
        print("  ℹ  Queue is empty.")
        return
    print(f"\n  📋 Processing {len(queue)} queued URL(s)...\n")
    remaining = []
    for item in queue:
        print(f"  ─── {item['url']}")
        ok = scrape_and_save(item["url"], item.get("output", output),
                             item.get("append", True), tracker, wait)
        if not ok:
            remaining.append(item)
    QUEUE_FILE.write_text(json.dumps(remaining, indent=2))
    print(f"\n  ✅ Done {len(queue)-len(remaining)}/{len(queue)}.")
    if remaining:
        print(f"  📋 {len(remaining)} still queued.")


# =============================================================================
# OUTPUT
# =============================================================================

def print_grants(grants: list, provider: str) -> None:
    label = {"groq": "Groq (llama3-70b)", "gemini": "Gemini Flash"}.get(provider, provider)
    sep   = "─" * 64
    for i, g in enumerate(grants, 1):
        print(f"\n{sep}")
        print(f"  Grant #{i}  [via {label}]")
        print(sep)
        print(f"  📌 Title        : {g.get('title','N/A')}")
        print(f"  🏢 Organization : {g.get('organization','N/A')}")
        print(f"  💰 Funding      : {g.get('funding_amount','N/A')}")
        print(f"  🎯 Theme        : {g.get('thematic_area','N/A')}")
        print(f"  📅 Deadline     : {g.get('deadline','N/A')}")
        print(f"  🌍 Country      : {g.get('country','N/A')}")
        print(f"  🔗 Source URL   : {g.get('source_url','N/A')}")
        desc = g.get("short_description", "N/A")
        words = desc.split()
        lines, line = [], []
        for w in words:
            line.append(w)
            if len(" ".join(line)) > 67:
                lines.append("                 " + " ".join(line)); line = []
        if line:
            lines.append("                 " + " ".join(line))
        print("  📝 Description  :\n" + "\n".join(lines))
    print(f"\n{sep}")
    print(f"  ✅ {len(grants)} grant(s) via {label}")
    print(sep)


def load_existing_grants(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_grants(grants: list, path: str, append: bool) -> None:
    all_grants = (load_existing_grants(path) if append else []) + grants
    with open(path, "w", encoding="utf-8") as f:
        json.dump(all_grants, f, ensure_ascii=False, indent=2)
    print(f"  💾 Saved {len(all_grants)} total → {path}")


# =============================================================================
# MAIN SCRAPE FUNCTION
# =============================================================================

def scrape_and_save(url: str, output: str, append: bool,
                    tracker: RateLimitTracker, wait: int,
                    cfg: ScrapeConfig = None) -> bool:
    """Full pipeline: analyse → user prompt → crawl → extract → dedup → save."""

    if not tracker.can_use("groq") and not tracker.can_use("gemini"):
        print("  🚫 Both limits exhausted.")
        queue_url(url, output, append)
        return False

    # ── Analyse + prompt (if no cfg passed in) ────────────────────────────────
    if cfg is None:
        try:
            site_info = analyse_site(url, wait_seconds=wait)
            cfg       = prompt_user(site_info, wait)
        except Exception as e:
            print(f"  ❌ Site analysis failed: {e}")
            return False

    print(f"\n  🚀 Starting crawl — {cfg.max_pages} page(s), "
          f"up to {cfg.max_items or 'all'} grants...\n")

    # ── Crawl ─────────────────────────────────────────────────────────────────
    try:
        pages = fetch_all_pages(url, cfg, wait_seconds=wait)
    except Exception as e:
        print(f"  ❌ Crawl failed: {e}")
        return False

    if not pages:
        print("  ⚠  No pages fetched.")
        return False

    # ── Extract ───────────────────────────────────────────────────────────────
    all_grants, last_provider = [], "none"

    for page_text, page_title, page_url in pages:
        print(f"\n  🔎 Extracting: {page_url}")
        if not tracker.can_use("groq") and not tracker.can_use("gemini"):
            print("  🚫 Limits hit mid-crawl — queuing remaining.")
            queue_url(page_url, output, True)
            continue
        grants, provider = extract_grants(page_text, page_url, tracker, getattr(cfg, 'providers', []))
        if grants:
            all_grants.extend(grants)
            last_provider = provider
            print(f"  ✔  {len(grants)} grant(s) on this page.")
        else:
            print("  ⚠  No grants on this page.")

    if not all_grants:
        print("\n  ⚠  No grants extracted.")
        return False

    # ── Dedup + cap at user-requested limit ───────────────────────────────────
    print(f"\n  🔍 Before dedup : {len(all_grants)}")
    all_grants = deduplicate_grants(all_grants, max_items=cfg.max_items)
    print(f"  ✅ After dedup  : {len(all_grants)}")

    print_grants(all_grants, last_provider)
    save_grants(all_grants, output, append)
    tracker.print_status()
    return True


# =============================================================================
# CLI
# =============================================================================

def run_scraper_frappe(config_name):
    import frappe
    
    # 1. Get Crawler Config Document
    cfg_doc = frappe.get_doc("Crawler Config", config_name)
    url = cfg_doc.start_url
    max_pages = cfg_doc.max_pages or 10
    
    # 2. Get Single Settings Document
    from . import config
    settings = frappe.get_single("Universal Crawler Settings")
    
    # Temporarily override settings dynamically!
    config.MAX_PAGINATION_PAGES = getattr(settings, 'max_pagination_pages', 50) or 50
    config.MAX_SCROLL_ATTEMPTS = getattr(settings, 'max_scroll_attempts', 20) or 20
    config.REQUEST_TIMEOUT = getattr(settings, 'request_timeout_seconds', 30) or 30
    
    from .api import log_to_frappe
    
    # We monkey patch python's print to stream to Frappe
    import builtins
    original_print = builtins.print
    def frappe_print(*args, **kwargs):
        line = " ".join(str(a) for a in args)
        log_to_frappe(config_name, line)
        original_print(*args, **kwargs)
    builtins.print = frappe_print
    
    try:
        log_to_frappe(config_name, f"🔬 Analysing site: {url}")
        
        # Run the site analyser
        site_info = analyse_site(url, wait_seconds=config.PAGE_LOAD_WAIT_SECONDS)
        
        # Compile Scrape Config (No interactive prompts here, fully automated based on DB config!)
        parsed_config = ScrapeConfig(
            max_pages=max_pages,
            max_items=0,
            enable_scroll=site_info.site_type in ("infinite_scroll", "single_page"),
            site_info=site_info
        )
        
        # Print the beautiful UI summary directly to Frappe
        print("──────────────────────────────────────────────────────")
        print("📊  Site Analysis Results")
        print("──────────────────────────────────────────────────────")
        print(f"🔗  URL        : {url}")
        print(f"📌  Title      : {site_info.first_page_title}")
        if site_info.site_type == "paginated":
            print(f"🗂  Site type  : Paginated")
            print(f"📄  Total pages: {site_info.total_pages or 'Unknown'}")
            print(f"📦  Total grants: ~{site_info.total_items or 'Unknown'}  (~{site_info.items_per_page or 'Unknown'} per page)")
        elif site_info.site_type in ("infinite_scroll", "single_page"):
            print(f"🗂  Site type  : {site_info.site_type.replace('_',' ').title()}")
            print(f"📦  Total grants available: ~{site_info.total_items or 'Unknown'}")
        else:
            print(f"🗂  Site type  : Static / Standard")
        print("──────────────────────────────────────────────────────\n")
        print(f"✅ Will scrape up to {parsed_config.max_pages} pages/items based on Crawler Config\n")
        print(f"🚀 Starting crawl — {max_pages} page(s)...\n")
        
        # Prepare dynamic configuration from Frappe Database
        active_providers = []
        for row in sorted(settings.llm_providers, key=lambda x: x.priority):
            if row.active:
                active_providers.append({
                    "provider_name": row.provider_name,
                    "model_name": row.model_name,
                    "api_key": row.get_password('api_key') or row.api_key
                })
                
        if not active_providers:
            log_to_frappe(config_name, "❌ Fatal Error: No active providers configured in Universal Crawler Settings!")
            return
            
        tracker = RateLimitTracker(providers=active_providers)
    
        # 3. Crawl all pages according to limits!
        pages = fetch_all_pages(url, parsed_config, wait_seconds=config.PAGE_LOAD_WAIT_SECONDS)
        
        # 4. Extract Data with dynamic LLM Fallbacks and save immediately!
        for page_text, page_title, page_url in pages:
            grants, provider = extract_grants(page_text, page_url, tracker, active_providers)
            
            from .api import push_grant_to_frappe, update_credits_frappe
            for grant in grants:
                push_grant_to_frappe(config_name, grant)
                
            # Stream the credit usage state!
            update_credits_frappe(config_name, f"Status: {tracker.status_line()}")
            
    finally:
        builtins.print = original_print


if __name__ == "__main__":
    main()