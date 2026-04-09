"""
site_analyser.py — Analyses a grant site before scraping.

Detects:
  - Site type: paginated | infinite-scroll | load-more | single-page
  - Total pages available (for paginated sites)
  - Estimated total grants/items available
  - Grants per page

Then interactively asks the user:
  "X grants available across Y pages. How many do you want to scrape?"

Returns a ScrapeConfig that the main scraper uses.
"""

import re
import math
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from . import config

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Run: pip install playwright && playwright install chromium")
    raise


# =============================================================================
# Data classes
# =============================================================================

@dataclass
class SiteInfo:
    site_type:          str    # "paginated" | "infinite_scroll" | "load_more" | "single_page"
    total_pages:        int    # total pages found (0 = unknown)
    total_items:        int    # estimated total grants/items (0 = unknown)
    items_per_page:     int    # grants visible per page (0 = unknown)
    first_page_text:    str    # extracted text from page 1 (reused by scraper)
    first_page_title:   str
    page_url:           str


@dataclass
class ScrapeConfig:
    max_pages:          int    # how many pages to crawl
    max_items:          int    # stop after this many grants (0 = no limit)
    enable_scroll:      bool
    site_info:          SiteInfo


# =============================================================================
# Pagination detector
# =============================================================================

NEXT_SELECTORS = [
    'a[rel="next"]',
    'a:has-text("Next")',
    'button:has-text("Next")',
    '.next a',
    '.pagination-next',
    '[aria-label="Next page"]',
    '[aria-label="next"]',
]

LOAD_MORE_SELECTORS = [
    'button:has-text("Load More")',
    'button:has-text("Load more")',
    'button:has-text("Show More")',
    'button:has-text("Show more")',
    'button:has-text("View More")',
    '[class*="load-more"]',
    '[class*="loadmore"]',
]


def _has_element(page, selectors: list) -> bool:
    for sel in selectors:
        try:
            if page.locator(sel).first.is_visible(timeout=600):
                return True
        except Exception:
            pass
    return False


def _count_total_pages(page, current_url: str) -> int:
    """
    Tries multiple strategies to find the total page count.
    Returns 0 if unknown.
    """
    # Strategy 1: last numbered page link
    try:
        count = page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href]'));
            const nums = links
                .map(a => parseInt(a.innerText.trim()))
                .filter(n => !isNaN(n) && n > 0);
            return nums.length ? Math.max(...nums) : 0;
        }""")
        if count and count > 1:
            return count
    except Exception:
        pass

    # Strategy 2: text like "Page 1 of 67" or "1 / 67"
    try:
        body = page.inner_text("body")
        patterns = [
            r"page\s+\d+\s+of\s+(\d+)",
            r"\d+\s*/\s*(\d+)\s*pages?",
            r"showing\s+\d+[–\-]\d+\s+of\s+(\d+)",
            r"(\d+)\s+pages?\s+total",
            r"total\s+pages?:?\s*(\d+)",
        ]
        for pat in patterns:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                if n > 1:
                    return n
    except Exception:
        pass

    # Strategy 3: aria-label like "Go to page 67"
    try:
        count = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('[aria-label]'));
            const nums = els
                .map(el => {
                    const m = el.getAttribute('aria-label').match(/page (\\d+)/i);
                    return m ? parseInt(m[1]) : 0;
                })
                .filter(n => n > 0);
            return nums.length ? Math.max(...nums) : 0;
        }""")
        if count and count > 1:
            return count
    except Exception:
        pass

    return 0


def _count_total_items(page) -> int:
    """
    Looks for text like '1,234 grants', '150 results', '47 opportunities'.
    Returns 0 if unknown.
    """
    try:
        body = page.inner_text("body")
        patterns = [
            r"([\d,]+)\s+grants?",
            r"([\d,]+)\s+results?",
            r"([\d,]+)\s+opportunities",
            r"([\d,]+)\s+funding",
            r"([\d,]+)\s+awards?",
            r"showing\s+\d+[–\-]\d+\s+of\s+([\d,]+)",
            r"total[:\s]+([\d,]+)",
            r"([\d,]+)\s+items?",
            r"found\s+([\d,]+)",
        ]
        for pat in patterns:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                val = int(m.group(1).replace(",", ""))
                if val > 0:
                    return val
    except Exception:
        pass
    return 0


def _count_items_on_page(page) -> int:
    """
    Heuristic: count grant-like cards/list items on the current page.
    """
    try:
        count = page.evaluate("""() => {
            const candidates = [
                'article', '.grant', '.opportunity', '.result',
                '.card', '[class*="grant"]', '[class*="result"]',
                '[class*="item"]', 'li.item', '.funding-item',
                '[class*="opportunity"]',
            ];
            for (const sel of candidates) {
                const els = document.querySelectorAll(sel);
                if (els.length > 2) return els.length;
            }
            return 0;
        }""")
        return count or 0
    except Exception:
        return 0


def _detect_infinite_scroll(page) -> bool:
    """
    Detects infinite scroll by scrolling slightly and checking if
    new content appears without a pagination or load-more button.
    """
    try:
        before = page.evaluate("() => document.body.scrollHeight")
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight * 0.7)")
        page.wait_for_timeout(2000)
        after = page.evaluate("() => document.body.scrollHeight")
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        return after > before * 1.05   # >5% growth = infinite scroll
    except Exception:
        return False


# =============================================================================
# Main analyser
# =============================================================================

def analyse_site(url: str, wait_seconds: int = config.PAGE_LOAD_WAIT_SECONDS) -> SiteInfo:
    """
    Loads the page, detects site type, counts pages and items.
    Returns a SiteInfo. Also returns first page text so we don't reload it.
    """
    print(f"\n  🔬 Analysing site: {url}")
    print("  ⏳ Loading page...")

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

        try:
            page.goto(url, wait_until="networkidle",
                      timeout=config.REQUEST_TIMEOUT * 1000)
        except PWTimeout:
            print("  ⚠  Page load timed out — using partial content.")

        page.wait_for_timeout(wait_seconds * 1000)
        title = page.title()

        # ── Detect site type ─────────────────────────────────────────────────
        has_next      = _has_element(page, NEXT_SELECTORS)
        has_load_more = _has_element(page, LOAD_MORE_SELECTORS)
        is_infinite   = False
        site_type     = "single_page"

        if has_next:
            site_type = "paginated"
        elif has_load_more:
            site_type = "load_more"
        else:
            is_infinite = _detect_infinite_scroll(page)
            if is_infinite:
                site_type = "infinite_scroll"

        # ── Count pages and items ────────────────────────────────────────────
        total_pages = 0
        total_items = 0
        items_per_page = 0

        if site_type == "paginated":
            total_pages    = _count_total_pages(page, url)
            total_items    = _count_total_items(page)
            items_per_page = _count_items_on_page(page)
            # Estimate total items if not found directly
            if total_items == 0 and total_pages > 0 and items_per_page > 0:
                total_items = total_pages * items_per_page

        elif site_type in ("infinite_scroll", "load_more"):
            total_items    = _count_total_items(page)
            items_per_page = _count_items_on_page(page)

        elif site_type == "single_page":
            total_pages    = 1
            total_items    = _count_items_on_page(page)
            items_per_page = total_items

        # ── Extract first page text ──────────────────────────────────────────
        page.evaluate("""() => {
            ['script','style','noscript','nav','footer','header',
             'iframe','svg','aside','form'].forEach(tag => {
                document.querySelectorAll(tag).forEach(el => el.remove());
            });
        }""")
        first_page_text = page.inner_text("body")
        first_page_text = re.sub(r"\n{3,}", "\n\n", first_page_text)
        first_page_text = re.sub(r"[ \t]{2,}", " ", first_page_text)

        browser.close()

    info = SiteInfo(
        site_type      = site_type,
        total_pages    = total_pages,
        total_items    = total_items,
        items_per_page = items_per_page,
        first_page_text= first_page_text.strip(),
        first_page_title= title,
        page_url       = url,
    )

    return info


# =============================================================================
# Interactive prompt
# =============================================================================

def _print_divider():
    print("  " + "─" * 54)


def prompt_user(info: SiteInfo, wait: int) -> ScrapeConfig:
    """
    Prints a summary of what was found and asks the user
    how much data they want to scrape. Returns a ScrapeConfig.
    """
    print()
    _print_divider()
    print(f"  📊  Site Analysis Results")
    _print_divider()
    print(f"  🔗  URL        : {info.page_url}")
    print(f"  📌  Title      : {info.first_page_title}")
    print(f"  🗂  Site type  : {info.site_type.replace('_', ' ').title()}")

    if info.site_type == "paginated":
        pages_label = str(info.total_pages) if info.total_pages else "Unknown"
        items_label = f"~{info.total_items:,}" if info.total_items else "Unknown"
        per_pg      = f"~{info.items_per_page}" if info.items_per_page else "Unknown"
        print(f"  📄  Total pages: {pages_label}")
        print(f"  📦  Total grants: {items_label}  ({per_pg} per page)")
    else:
        items_label = f"~{info.total_items:,}" if info.total_items else "Unknown"
        print(f"  📦  Total grants available: {items_label}")

    _print_divider()
    print()

    # ── Ask how much to scrape ────────────────────────────────────────────────
    if info.site_type == "paginated" and info.total_pages:
        # Ask for number of pages
        while True:
            try:
                raw = input(
                    f"  ❓ How many pages do you want to scrape? "
                    f"[1–{info.total_pages}, or press Enter for all]: "
                ).strip()
                if raw == "":
                    max_pages = info.total_pages
                    break
                val = int(raw)
                if 1 <= val <= info.total_pages:
                    max_pages = val
                    break
                print(f"  ⚠  Please enter a number between 1 and {info.total_pages}.")
            except ValueError:
                print("  ⚠  Please enter a valid number.")

        # Estimate grants from chosen pages
        max_items = (
            max_pages * info.items_per_page if info.items_per_page else 0
        )
        estimated = f"~{max_items:,}" if max_items else "unknown number of"
        print(f"\n  ✅ Will scrape {max_pages} page(s)  ({estimated} grants)")

        return ScrapeConfig(
            max_pages     = max_pages,
            max_items     = max_items,
            enable_scroll = False,
            site_info     = info,
        )

    else:
        # Infinite scroll / load-more / single page or unknown pages
        # Ask for number of grants
        total_label = f"{info.total_items:,}" if info.total_items else "an unknown number of"

        while True:
            try:
                prompt_str = (
                    f"  ❓ {total_label} grants available. "
                    f"How many do you want to scrape? "
                    f"[number, or Enter for all]: "
                )
                raw = input(prompt_str).strip()

                if raw == "":
                    max_items = info.total_items or 0
                    # Estimate pages needed
                    if info.items_per_page and max_items:
                        max_pages = math.ceil(max_items / info.items_per_page)
                    else:
                        max_pages = config.MAX_PAGINATION_PAGES
                    break

                val = int(raw)
                if val < 1:
                    print("  ⚠  Please enter a number greater than 0.")
                    continue

                max_items = val
                if info.items_per_page:
                    max_pages = math.ceil(val / info.items_per_page)
                    max_pages = max(1, max_pages)
                else:
                    max_pages = config.MAX_PAGINATION_PAGES
                break

            except ValueError:
                print("  ⚠  Please enter a valid number.")

        print(f"\n  ✅ Will scrape up to {max_items or 'all'} grants")

        return ScrapeConfig(
            max_pages     = max_pages,
            max_items     = max_items,
            enable_scroll = info.site_type in ("infinite_scroll", "single_page"),
            site_info     = info,
        )
