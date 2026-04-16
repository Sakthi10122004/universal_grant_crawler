import re
import math
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


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
    # Strategy 1: JS pagination number links
    try:
        count = page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href], button'));
            const nums = links
                .map(x => x.innerText.trim())
                .filter(txt => /^\\d+$/.test(txt))  // strictly just digits
                .map(txt => parseInt(txt, 10))
                .filter(n => !isNaN(n) && n > 0 && n < 10000); // sanity limit
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
            r"page[ \t\xa0]+\d+[ \t\xa0]+of[ \t\xa0]+(\d+)",
            r"\d+[ \t\xa0]*/[ \t\xa0]*(\d+)[ \t\xa0]*pages?",
            r"showing[ \t\xa0]+\d+[–\-]\d+[ \t\xa0]+of[ \t\xa0]+(\d+)",
            r"(\d+)[ \t\xa0]+pages?[ \t\xa0]+total",
            r"total[ \t\xa0]+pages?:?[ \t\xa0]*(\d+)",
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
            r"([\d,]+)[ \t\xa0]+grants?",
            r"([\d,]+)[ \t\xa0]+results?",
            r"([\d,]+)[ \t\xa0]+opportunities",
            r"([\d,]+)[ \t\xa0]+funding",
            r"([\d,]+)[ \t\xa0]+awards?",
            r"showing[ \t\xa0]+\d+[–\-]\d+[ \t\xa0]+of[ \t\xa0]+([\d,]+)",
            r"total[: \t\xa0]+([\d,]+)",
            r"([\d,]+)[ \t\xa0]+items?",
            r"found[ \t\xa0]+([\d,]+)",
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

def analyse_site(url: str, wait_seconds: int = 4) -> SiteInfo:
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
                      timeout=30_000)
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


