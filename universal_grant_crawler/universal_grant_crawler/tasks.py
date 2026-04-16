import frappe
from datetime import datetime, date


def delete_expired_grants():
    """Daily scheduled task: delete all Crawled Grant Records whose deadline has passed.

    Runs via hooks.py → scheduler_events → daily.
    """
    # Fetch all grants that are currently marked as Expired
    expired = frappe.get_all(
        "Crawled Grant Record",
        filters={"status": "Expired"},
        fields=["name", "title", "deadline"],
    )

    if not expired:
        frappe.logger().info("🗑 Expired grant cleanup: nothing to delete.")
        return

    count = 0
    for grant in expired:
        try:
            frappe.delete_doc("Crawled Grant Record", grant["name"], force=True)
            count += 1
        except Exception as e:
            frappe.logger().error(f"Failed to delete expired grant '{grant['name']}': {e}")

    frappe.db.commit()
    frappe.logger().info(f"🗑 Expired grant cleanup: deleted {count}/{len(expired)} expired grants.")


def recheck_grant_statuses():
    """Re-evaluate status for all grants with a parseable deadline.

    Catches grants whose deadline just passed since last crawl.
    Called daily before delete_expired_grants.
    """
    grants = frappe.get_all(
        "Crawled Grant Record",
        filters={"status": "Active"},
        fields=["name", "deadline"],
    )

    today = date.today()
    newly_expired = 0

    for g in grants:
        deadline_str = (g.get("deadline") or "").strip()
        if not deadline_str or deadline_str.lower() in ("not specified", "rolling", "open", "ongoing", "tbd"):
            continue

        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                deadline_date = datetime.strptime(deadline_str, fmt).date()
                if deadline_date < today:
                    frappe.db.set_value("Crawled Grant Record", g["name"], "status", "Expired")
                    newly_expired += 1
                break
            except ValueError:
                continue

    if newly_expired:
        frappe.db.commit()
        frappe.logger().info(f"🔄 Recheck: marked {newly_expired} grant(s) as Expired.")
