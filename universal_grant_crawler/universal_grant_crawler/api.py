import frappe
import traceback

# Import the existing scripts from this folder!
from .grant_scraper import run_scraper_frappe


@frappe.whitelist()
def start_headless_crawl(config_name=None, **kwargs):
    if not config_name:
        config_name = frappe.form_dict.get("config_name")

    # Reset logs + status before starting
    frappe.db.set_value("Crawler Config", config_name, {
        "status": "Running",
        "logs": "",
        "remaining_credits": ""
    })
    frappe.db.commit()

    # Enqueue Playwright scraper in the background worker queue!
    frappe.enqueue(
        "universal_grant_crawler.universal_grant_crawler.api.execute_crawl",
        queue="long",
        timeout=3600,  # 1 hour timeout
        config_name=config_name
    )
    return "Queued"


def execute_crawl(config_name):
    try:
        run_scraper_frappe(config_name)
        frappe.db.set_value("Crawler Config", config_name, "status", "Done")
    except Exception as e:
        error_trace = traceback.format_exc()
        # Append traceback to existing logs
        existing = frappe.db.get_value("Crawler Config", config_name, "logs") or ""
        frappe.db.set_value("Crawler Config", config_name, {
            "status": "Failed",
            "logs": existing + "\n\n❌ FATAL ERROR:\n" + error_trace
        })
    finally:
        frappe.db.commit()


def push_grant_to_frappe(config_name, grant):
    """Called dynamically by the script every time a grant is extracted. Skips duplicates."""
    title = grant.get("title", "Unknown")

    # Skip if a record with this exact title already exists
    if frappe.db.exists("Crawled Grant Record", title):
        print(f"  ⏭  Skipping duplicate: {title}")
        return

    doc = frappe.get_doc({
        "doctype": "Crawled Grant Record",
        "crawler_config": config_name,
        "title": title,
        "organization": grant.get("organization"),
        "grant_amount": grant.get("funding_amount"),
        "thematic_area": grant.get("thematic_area"),
        "deadline": grant.get("deadline"),
        "description": grant.get("short_description"),
        "country": grant.get("country"),
        "source_url": (grant.get("source_url") or "")[:900]  # Safety truncation
    })
    doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
    frappe.db.commit()
    print(f"  💾 Saved: {title}")


def log_to_frappe(config_name, log_text):
    """Efficiently appends a log line into Crawler Config without loading the whole doc."""
    existing = frappe.db.get_value("Crawler Config", config_name, "logs") or ""
    frappe.db.set_value("Crawler Config", config_name, "logs", existing + "\n" + log_text)
    frappe.db.commit()


def update_credits_frappe(config_name, tracker_string):
    """Updates the credit tracker display field in Frappe UI."""
    frappe.db.set_value("Crawler Config", config_name, "remaining_credits", tracker_string)
    frappe.db.commit()
