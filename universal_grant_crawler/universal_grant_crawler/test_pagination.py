from playwright.sync_api import sync_playwright

url = "https://simpler.grants.gov/search"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.goto(url)
    page.wait_for_timeout(5000)
    print("Page title:", page.title())

    # find Next button
    for sel in ['a:has-text("Next")', 'button:has-text("Next")', '[aria-label="Next page"]', '[aria-label="next"]', '.usa-pagination__next']:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1000):
                print(f"Found next button with selector: {sel}")
                print("HTML:", el.evaluate("el => el.outerHTML"))
                break
        except Exception:
            pass
    browser.close()
