import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

# ── Scraper defaults (overridden at runtime from Frappe DB in run_scraper_frappe) ──
PAGE_LOAD_WAIT_SECONDS = 4
REQUEST_TIMEOUT        = 30      # Playwright page load timeout (seconds)
MAX_CONTENT_CHARS      = 10_000  # Default max page text sent to LLM
RETRY_ATTEMPTS         = 2
RETRY_DELAY_SECONDS    = 3
MAX_PAGINATION_PAGES   = 500
MAX_SCROLL_ATTEMPTS    = 30
MAX_LOAD_MORE_CLICKS   = 20
DEFAULT_OUTPUT_FILE    = "grants.json"
RATE_LIMIT_STATE_FILE  = ".rate_limit_state.json"
class FrappeDBTracker:
    def __init__(self, providers):
        self.providers = providers
        
    def can_use(self, provider_name: str) -> bool:
        try:
            from .api import get_provider_credits
            credits = get_provider_credits()
            info = credits.get(provider_name)
            if not info: return True
            return info["remaining"] > 0
        except Exception:
            return True

    def any_available(self) -> bool:
        try:
            from .api import get_provider_credits
            credits = get_provider_credits()
            if not credits: return True
            return any(info["remaining"] > 0 for info in credits.values())
        except Exception:
            return True

    def increment(self, provider_name: str) -> None:
        # Frappe DB handles this automatically via log_llm_usage
        pass

    def status_line(self) -> str:
        try:
            from .api import get_provider_credits
            credits = get_provider_credits()
            parts = [f"{pname}: {info['used']}/{info['limit']}" for pname, info in credits.items()]
            return "  |  ".join(parts) if parts else "No providers tracking"
        except Exception:
            return "Frappe DB Tracking Mode"

    def print_status(self) -> None:
        print(f"\n  📊 Daily usage → {self.status_line()}")

from .site_analyser import analyse_site, ScrapeConfig

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
    max_scrolls      = MAX_SCROLL_ATTEMPTS
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
    limit       = max_clicks or MAX_LOAD_MORE_CLICKS

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


def advance_to_next_page(page, wait_seconds: int, start_url: str = None) -> bool:
    """Clicks the next page button. Returns True if successful, False if no button.

    Guards against greedy selectors (e.g. 'a:has-text("Next")') that match
    card-level links and accidentally navigate into a detail page.  After each
    click we compare the new URL's path prefix against the listing base path;
    if they diverge we go back and try the next selector.
    """
    from urllib.parse import urlparse

    # Derive the base path of the search/listing page so we can detect
    # when a click navigated us away from the results (e.g. into /opportunity/…).
    listing_path = urlparse(start_url).path.rstrip("/") if start_url else None

    url_before = page.url

    for label, selector in NEXT_PAGE_SELECTORS:
        try:
            el = page.locator(selector).first
            if not el.is_visible(timeout=500):
                continue

            print(f"  ➡  Next page [{label}] clicked.")
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            el.click(timeout=3000)

            page.wait_for_timeout(wait_seconds * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=3000)
            except PWTimeout:
                pass

            url_after = page.url

            # ── Guard: did we land on a detail/sub-page instead of the next listing page? ──
            if listing_path and url_after != url_before:
                after_path = urlparse(url_after).path.rstrip("/")
                # Accept if the new path starts with the listing base path
                # (e.g. /search?page=2 is fine; /opportunity/abc-123 is not).
                if not after_path.startswith(listing_path) and after_path != listing_path:
                    print(f"  ⚠  Selector [{label}] navigated to a detail page "
                          f"({url_after}) — going back and trying next selector.")
                    try:
                        page.go_back()
                        page.wait_for_timeout(wait_seconds * 1000)
                    except Exception:
                        pass
                    continue  # try the next selector

            if url_after != url_before:
                return True

            # URL didn't change — selector may have matched a disabled button; continue
            print(f"  ⚠  Selector [{label}] clicked but URL unchanged — skipping.")

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

        unchanged_streak = 0

        while current_url and page_num <= cfg.max_pages:
            # Show a sensible progress label
            if cfg.max_pages >= 99999:
                print(f"\n  🌐 Page {page_num} (all pages): {current_url}")
            else:
                print(f"\n  🌐 Page {page_num}/{cfg.max_pages}: {current_url}")

            if page_num == 1:
                try:
                    page.goto(current_url, wait_until="networkidle",
                              timeout=REQUEST_TIMEOUT * 1000)
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
                unchanged_streak += 1
                print(f"  ⚠  Content unchanged (streak {unchanged_streak}/2) — "
                      f"may be SPA end of pagination.")
                if unchanged_streak >= 2:
                    print("  🛑 Content unchanged twice in a row — stopping.")
                    break
            else:
                unchanged_streak = 0
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
                success = advance_to_next_page(page, wait_seconds, start_url=start_url)
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
    return MAX_LOAD_MORE_CLICKS


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


def extract_with_provider(text: str, url: str, p: dict, max_chars: int = None) -> list:
    """Call a single LLM provider to extract grants from page text.
    
    Args:
        text: Full page text.
        url: Page URL for context.
        p: Provider dict with provider_name, model_name, api_key.
        max_chars: Max characters of page content to include in prompt.
                   Falls back to provider's max_content_chars or global default.
    """
    name = p["provider_name"].lower()
    char_limit = max_chars or p.get("max_content_chars") or MAX_CONTENT_CHARS
    prompt = EXTRACT_PROMPT.format(url=url, content=text[:char_limit])
    
    if name == "groq":
        from groq import Groq
        client = Groq(api_key=p["api_key"])
        print(f"  🤖 Groq ({p['model_name']}): extracting ({char_limit} chars)...")
        resp = client.chat.completions.create(
            model=p["model_name"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8192, temperature=0.1,
        )
        return _parse_llm_response(resp.choices[0].message.content, url)
        
    elif name == "gemini":
        from google import genai
        client = genai.Client(api_key=p["api_key"])
        print(f"  🤖 Gemini ({p['model_name']}): extracting ({char_limit} chars)...")
        resp = client.models.generate_content(model=p["model_name"], contents=prompt)
        return _parse_llm_response(resp.text, url)
        
    elif name == "openai" or name == "anthropic":
        from openai import OpenAI
        client = OpenAI(api_key=p["api_key"])
        print(f"  🤖 {p['provider_name']} ({p['model_name']}): extracting ({char_limit} chars)...")
        resp = client.chat.completions.create(
            model=p["model_name"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096, temperature=0.1,
        )
        return _parse_llm_response(resp.choices[0].message.content, url)
        
    elif name == "ollama":
        import requests
        print(f"  🤖 Ollama ({p['model_name']}): extracting locally ({char_limit} chars)...")
        resp = requests.post("http://localhost:11434/api/chat", json={
            "model": p["model_name"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 4096}
        })
        return _parse_llm_response(resp.json()["message"]["content"], url)
        
    else:
        print(f"  ⚠  Unknown provider type: {name}")
        return []


def _is_content_too_large_error(error: Exception) -> bool:
    """Check if the error is due to content/request being too large."""
    err_str = str(error).lower()
    return any(indicator in err_str for indicator in [
        "413", "too large", "request too large", "tokens per minute",
        "token limit", "context length", "maximum context",
    ])


def extract_grants(page_text: str, url: str, tracker: 'FrappeDBTracker',
                    providers: list, crawler_config: str = None) -> tuple:
    """Extract grants using LLM providers with automatic failover.
    
    Strategy:
      1. Try each provider in priority order.
      2. If a provider fails with content-too-large → auto-reduce content and retry ONCE.
      3. If a provider fails for any other reason → immediately switch to next provider.
      4. After all providers tried, do one final retry pass with delay.
      5. Only return empty if ALL providers have been exhausted.
    
    Every API call (success or failure) is logged to LLM Usage Log DocType.
    """
    if not providers:
        print("  ❌ No LLM providers configured!")
        return [], "none"

    # Import usage logger (only available inside Frappe context)
    _log_usage = None
    try:
        from .api import log_llm_usage
        _log_usage = log_llm_usage
    except Exception:
        pass  # CLI mode — no Frappe logging

    permanently_failed = set()  # Providers that failed even with reduced content

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        if attempt > 1:
            print(f"  🔁 Retry attempt {attempt}/{RETRY_ATTEMPTS} — trying all providers again...")
            time.sleep(RETRY_DELAY_SECONDS)

        for p in providers:
            prov_name = p["provider_name"]

            # Skip providers that permanently failed this extraction
            if prov_name in permanently_failed:
                continue

            # Skip providers that hit their daily rate limit
            if not tracker.can_use(prov_name):
                print(f"  ⚡ {prov_name} daily limit reached, skipping.")
                if _log_usage:
                    _log_usage(prov_name, p.get("model_name", ""), "Rate Limited",
                               crawler_config=crawler_config, page_url=url)
                continue

            max_chars = p.get("max_content_chars") or MAX_CONTENT_CHARS

            # ── Attempt extraction with timing ──
            t_start = time.time()
            try:
                grants = extract_with_provider(page_text, url, p, max_chars)
                elapsed_ms = int((time.time() - t_start) * 1000)

                if grants:
                    tracker.increment(prov_name)
                    print(f"  ✔  {prov_name}: {len(grants)} grant(s) extracted. ({elapsed_ms}ms)")
                    if _log_usage:
                        _log_usage(prov_name, p.get("model_name", ""), "Success",
                                   crawler_config=crawler_config, page_url=url,
                                   content_chars_sent=max_chars,
                                   grants_extracted=len(grants),
                                   response_time_ms=elapsed_ms)
                    return grants, prov_name
                else:
                    print(f"  ⚠  {prov_name}: returned empty, trying next provider...")
                    if _log_usage:
                        _log_usage(prov_name, p.get("model_name", ""), "Failed",
                                   crawler_config=crawler_config, page_url=url,
                                   content_chars_sent=max_chars,
                                   response_time_ms=elapsed_ms,
                                   error_message="LLM returned empty response")

            except Exception as e:
                elapsed_ms = int((time.time() - t_start) * 1000)
                print(f"  ⚠  {prov_name} Error: {e}")

                # ── Handle content-too-large: reduce and retry once ──
                if _is_content_too_large_error(e):
                    if _log_usage:
                        _log_usage(prov_name, p.get("model_name", ""), "Content Too Large",
                                   crawler_config=crawler_config, page_url=url,
                                   content_chars_sent=max_chars,
                                   response_time_ms=elapsed_ms,
                                   error_message=str(e)[:500])

                    reduced_chars = max(2000, max_chars // 2)
                    print(f"  📏 Content too large for {prov_name}. "
                          f"Retrying with {reduced_chars} chars (was {max_chars})...")
                    t2 = time.time()
                    try:
                        grants = extract_with_provider(page_text, url, p, reduced_chars)
                        elapsed_ms2 = int((time.time() - t2) * 1000)
                        if grants:
                            tracker.increment(prov_name)
                            print(f"  ✔  {prov_name}: {len(grants)} grant(s) (reduced content, {elapsed_ms2}ms).")
                            if _log_usage:
                                _log_usage(prov_name, p.get("model_name", ""), "Success",
                                           crawler_config=crawler_config, page_url=url,
                                           content_chars_sent=reduced_chars,
                                           grants_extracted=len(grants),
                                           response_time_ms=elapsed_ms2)
                            return grants, prov_name
                    except Exception as e2:
                        elapsed_ms2 = int((time.time() - t2) * 1000)
                        print(f"  ⚠  {prov_name} still failed after reducing content: {e2}")
                        permanently_failed.add(prov_name)
                        if _log_usage:
                            _log_usage(prov_name, p.get("model_name", ""), "Failed",
                                       crawler_config=crawler_config, page_url=url,
                                       content_chars_sent=reduced_chars,
                                       response_time_ms=elapsed_ms2,
                                       error_message=str(e2)[:500])
                else:
                    # Non-content error
                    if _log_usage:
                        _log_usage(prov_name, p.get("model_name", ""), "Failed",
                                   crawler_config=crawler_config, page_url=url,
                                   content_chars_sent=max_chars,
                                   response_time_ms=elapsed_ms,
                                   error_message=str(e)[:500])

                # Always try the next provider after any error
                print(f"  ➡  Switching to next provider...")
                continue

    # All attempts exhausted
    print(f"  ❌ All {len(providers)} configured provider(s) failed. "
          f"No grants extracted from this page.")
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


def process_queue(tracker: 'FrappeDBTracker', output: str, append: bool, wait: int) -> None:
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
    sep   = "─" * 64
    for i, g in enumerate(grants, 1):
        print(f"\n{sep}")
        print(f"  Grant #{i}  [via {provider}]")
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
    print(f"  ✅ {len(grants)} grant(s) via {provider}")
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
                    tracker: 'FrappeDBTracker', wait: int,
                    cfg: ScrapeConfig = None) -> bool:
    """Full pipeline: analyse → user prompt → crawl → extract → dedup → save."""

    if not tracker.any_available():
        print("  🚫 All provider limits exhausted.")
        queue_url(url, output, append)
        return False

    # ── Analyse + prompt (if no cfg passed in) ────────────────────────────────
    if cfg is None:
        try:
            site_info = analyse_site(url, wait_seconds=wait)
            cfg = ScrapeConfig(
                max_pages=50,
                max_items=0,
                enable_scroll=site_info.site_type in ("infinite_scroll", "single_page"),
                site_info=site_info
            )
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
        if not tracker.any_available():
            print("  🚫 All provider limits hit mid-crawl — queuing remaining.")
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
    crawl_mode = getattr(cfg_doc, 'crawl_mode', 'Specific Pages') or 'Specific Pages'
    max_pages = cfg_doc.max_pages or 10
    
    # 2. Get Single Settings Document
    global MAX_PAGINATION_PAGES, MAX_SCROLL_ATTEMPTS, REQUEST_TIMEOUT
    settings = frappe.get_single("Universal Crawler Settings")

    # Override module-level defaults with values from Frappe DB
    MAX_PAGINATION_PAGES = getattr(settings, 'max_pagination_pages', 50) or 50
    MAX_SCROLL_ATTEMPTS  = getattr(settings, 'max_scroll_attempts', 20) or 20
    REQUEST_TIMEOUT      = getattr(settings, 'request_timeout_seconds', 30) or 30
    
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
        site_info = analyse_site(url, wait_seconds=PAGE_LOAD_WAIT_SECONDS)
        
        # ── Resolve max_pages based on crawl_mode ────────────────────────────
        if crawl_mode == "All Pages":
            # Use the detected total pages if available, otherwise a very
            # large number so the crawler keeps going until pagination ends.
            if site_info.total_pages and site_info.total_pages > 0:
                max_pages = site_info.total_pages
            else:
                max_pages = 99999   # effectively unlimited; stops when no next page is found
            pages_label = f"ALL ({max_pages})" if site_info.total_pages else "ALL (until no more pages)"
        else:
            pages_label = str(max_pages)
        
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
        print(f"🔧 Crawl mode  : {crawl_mode}")
        print(f"✅ Will scrape up to {pages_label} pages based on Crawler Config\n")
        print(f"🚀 Starting crawl — {pages_label} page(s)...\n")
        
        # Prepare dynamic configuration from Frappe Database
        active_providers = []
        for row in sorted(settings.llm_providers, key=lambda x: x.priority):
            if row.active:
                active_providers.append({
                    "provider_name": row.provider_name,
                    "model_name": row.model_name,
                    "api_key": row.get_password('api_key') or row.api_key,
                    "daily_limit": row.daily_limit or 100_000,
                    "max_content_chars": row.max_content_chars or MAX_CONTENT_CHARS,
                })
                
        if not active_providers:
            log_to_frappe(config_name, "❌ Fatal Error: No active providers configured in Universal Crawler Settings!")
            return

        # Log the provider chain for transparency
        chain = " → ".join(
            f"{p['provider_name']}({p['model_name']}, {p['max_content_chars']}chars)"
            for p in active_providers
        )
        print(f"🔗 LLM Provider chain: {chain}")
        print(f"   (will auto-failover to next provider on errors)\n")
            
        tracker = FrappeDBTracker(providers=active_providers)
    
        # 3. Crawl all pages according to limits!
        pages = fetch_all_pages(url, parsed_config, wait_seconds=PAGE_LOAD_WAIT_SECONDS)
        
        # 4. Extract Data with dynamic LLM Fallbacks and save immediately!
        from .api import push_grant_to_frappe, update_credits_frappe, get_provider_credits

        total_extracted = 0
        total_saved = 0
        total_updated = 0
        total_skipped = 0
        total_expired = 0

        for page_idx, (page_text, page_title, page_url) in enumerate(pages, 1):
            grants, provider = extract_grants(
                page_text, page_url, tracker, active_providers,
                crawler_config=config_name
            )

            page_saved = 0
            page_updated = 0
            page_skipped = 0
            page_expired = 0
            for grant in grants:
                result = push_grant_to_frappe(config_name, grant)
                if result == "saved":
                    page_saved += 1
                elif result == "updated":
                    page_updated += 1
                elif result == "expired":
                    page_expired += 1
                else:
                    page_skipped += 1

            total_extracted += len(grants)
            total_saved += page_saved
            total_updated += page_updated
            total_skipped += page_skipped
            total_expired += page_expired

            print(f"\n  📊 Page {page_idx}/{len(pages)} summary: "
                  f"{len(grants)} extracted, {page_saved} saved, {page_updated} updated, "
                  f"{page_skipped} skipped, "
                  f"{page_expired} expired")

            # Build accurate credit display from database
            try:
                credits = get_provider_credits()
                parts = []
                for pname, info in credits.items():
                    parts.append(f"{pname}: {info['used']}/{info['limit']} "
                                 f"({info['remaining']} left)")
                credit_str = "  |  ".join(parts) if parts else "No providers"
                update_credits_frappe(config_name, credit_str)
            except Exception:
                update_credits_frappe(config_name, f"Status: {tracker.status_line()}")

        # Final summary
        print(f"\n{'═' * 54}")
        print(f"  📈 CRAWL COMPLETE — FINAL SUMMARY")
        print(f"{'═' * 54}")
        print(f"  📄 Pages crawled   : {len(pages)}")
        print(f"  🤖 Grants extracted: {total_extracted}")
        print(f"  💾 Grants saved    : {total_saved}")
        print(f"  🔄 Grants updated  : {total_updated}")
        print(f"  ⏭  Skipped errors  : {total_skipped}")
        print(f"  ⏳ Expired (skipped): {total_expired}")
        print(f"{'═' * 54}")
            
    finally:
        builtins.print = original_print


if __name__ == "__main__":
    main()