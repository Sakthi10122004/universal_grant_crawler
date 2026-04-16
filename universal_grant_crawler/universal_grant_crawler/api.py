import frappe
import traceback
from datetime import datetime, timedelta

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
    """Called dynamically by the script every time a grant is extracted. Skips duplicates.
    Returns 'saved', 'skipped', or 'expired'."""
    from datetime import datetime, date

    title = grant.get("title", "Unknown")

    # ── Compute status from deadline ─────────────────────────────────────────
    status = "Active"
    deadline_str = (grant.get("deadline") or "").strip()
    if deadline_str and deadline_str.lower() not in ("not specified", "rolling", "open", "ongoing", "tbd"):
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                deadline_date = datetime.strptime(deadline_str, fmt).date()
                if deadline_date < date.today():
                    status = "Expired"
                break
            except ValueError:
                continue

    # Skip saving expired grants entirely
    if status == "Expired":
        print(f"  ⏭  Skipping expired grant: {title} (deadline: {deadline_str})")
        return "expired"

    # Check if a record with this exact title already exists
    existing_name = frappe.db.get_value("Crawled Grant Record", {"title": title}, "name")

    # Safety truncation for URL
    source_url_val = (grant.get("source_url") or "")[:900]

    if existing_name:
        try:
            doc = frappe.get_doc("Crawled Grant Record", existing_name)
            doc.crawler_config = config_name
            doc.organization = grant.get("organization")
            doc.grant_amount = grant.get("funding_amount")
            doc.thematic_area = grant.get("thematic_area")
            doc.status = status
            doc.deadline = grant.get("deadline")
            doc.description = grant.get("short_description")
            doc.country = grant.get("country")
            doc.source_url = source_url_val
            doc.save(ignore_permissions=True)
            frappe.db.commit()
            print(f"  🔄 Updated: {title}")
            return "updated"
        except Exception:
            frappe.clear_last_message()
            print(f"  ⚠  Failed to update: {title}")
            return "skipped"
    else:
        try:
            doc = frappe.get_doc({
                "doctype": "Crawled Grant Record",
                "crawler_config": config_name,
                "title": title,
                "organization": grant.get("organization"),
                "grant_amount": grant.get("funding_amount"),
                "thematic_area": grant.get("thematic_area"),
                "status": status,
                "deadline": grant.get("deadline"),
                "description": grant.get("short_description"),
                "country": grant.get("country"),
                "source_url": source_url_val
            })
            doc.insert(ignore_permissions=True)
            frappe.db.commit()
            print(f"  💾 Saved: {title}")
            return "saved"
        except frappe.UniqueValidationError:
            # Race condition: another worker inserted between our check and insert
            frappe.clear_last_message()
            print(f"  ⏭  Skipping duplicate (race): {title}")
            return "skipped"


def log_to_frappe(config_name, log_text):
    """Efficiently appends a log line into Crawler Config without loading the whole doc."""
    existing = frappe.db.get_value("Crawler Config", config_name, "logs") or ""
    frappe.db.set_value("Crawler Config", config_name, "logs", existing + "\n" + log_text)
    frappe.db.commit()


def update_credits_frappe(config_name, tracker_string):
    """Updates the credit tracker display field in Frappe UI."""
    frappe.db.set_value("Crawler Config", config_name, "remaining_credits", tracker_string)
    frappe.db.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# LLM Usage Logging — records every API call to the LLM Usage Log DocType
# ═══════════════════════════════════════════════════════════════════════════════

def log_llm_usage(provider_name, model_name, status, crawler_config=None,
                  page_url=None, content_chars_sent=0, grants_extracted=0,
                  response_time_ms=0, error_message=None):
    """Log a single LLM API call to the database.

    Called by grant_scraper after every extract_with_provider attempt.
    """
    try:
        doc = frappe.get_doc({
            "doctype": "LLM Usage Log",
            "provider_name": provider_name,
            "model_name": model_name,
            "status": status,
            "crawler_config": crawler_config,
            "page_url": (page_url or "")[:900],
            "content_chars_sent": content_chars_sent,
            "grants_extracted": grants_extracted,
            "response_time_ms": response_time_ms,
            "error_message": (str(error_message) if error_message else None),
        })
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        # Never let logging failures break the scraper
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard API — powers the LLM Usage Dashboard page
# ═══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def get_llm_usage_dashboard(period="Today"):
    """Returns all data needed for the LLM Usage Dashboard page."""

    # ── Determine date range ──────────────────────────────────────────────────
    today = datetime.now().date()
    if period == "Last 7 Days":
        start_date = today - timedelta(days=7)
    elif period == "Last 30 Days":
        start_date = today - timedelta(days=30)
    elif period == "All Time":
        start_date = None
    else:  # "Today"
        start_date = today

    date_filter = ""
    if start_date:
        date_filter = f"AND DATE(creation) >= '{start_date}'"

    # ── Totals ────────────────────────────────────────────────────────────────
    totals_sql = f"""
        SELECT
            COUNT(*) as total_requests,
            SUM(CASE WHEN status = 'Success' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status != 'Success' THEN 1 ELSE 0 END) as failed,
            COALESCE(SUM(grants_extracted), 0) as grants_extracted
        FROM `tabLLM Usage Log`
        WHERE 1=1 {date_filter}
    """
    totals_row = frappe.db.sql(totals_sql, as_dict=True)
    totals = totals_row[0] if totals_row else {}
    total_req = totals.get("total_requests") or 0
    successful = totals.get("successful") or 0
    totals["success_rate"] = f"{round((successful / total_req) * 100)}%" if total_req > 0 else "0%"

    # ── Per-provider breakdown ────────────────────────────────────────────────
    provider_sql = f"""
        SELECT
            provider_name,
            model_name,
            COUNT(*) as total_requests,
            SUM(CASE WHEN status = 'Success' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status != 'Success' THEN 1 ELSE 0 END) as failed,
            COALESCE(SUM(grants_extracted), 0) as grants_extracted
        FROM `tabLLM Usage Log`
        WHERE 1=1 {date_filter}
        GROUP BY provider_name, model_name
        ORDER BY total_requests DESC
    """
    provider_rows = frappe.db.sql(provider_sql, as_dict=True)

    # Enrich with config data from Universal Crawler Settings
    settings = None
    try:
        settings = frappe.get_single("Universal Crawler Settings")
    except Exception:
        pass

    provider_config = {}
    if settings and hasattr(settings, "llm_providers"):
        for row in settings.llm_providers:
            provider_config[row.provider_name] = {
                "active": bool(row.active),
                "daily_limit": row.daily_limit or 100000,
                "model_name": row.model_name,
            }

    # Today's usage for the progress bars (always today, regardless of period)
    # Only count actual API calls — exclude 'Rate Limited' which never hit the API
    today_usage_sql = """
        SELECT
            provider_name,
            COUNT(*) as used_today
        FROM `tabLLM Usage Log`
        WHERE DATE(creation) = CURDATE()
          AND status != 'Rate Limited'
        GROUP BY provider_name
    """
    today_usage_rows = frappe.db.sql(today_usage_sql, as_dict=True)
    today_usage = {r["provider_name"]: r["used_today"] for r in today_usage_rows}

    providers = []
    # Include all configured providers, even if they have zero usage
    seen = set()
    for row in provider_rows:
        name = row["provider_name"]
        seen.add(name)
        cfg = provider_config.get(name, {})
        providers.append({
            "provider_name": name,
            "model_name": row.get("model_name") or cfg.get("model_name", ""),
            "active": cfg.get("active", True),
            "daily_limit": cfg.get("daily_limit", 100000),
            "used": today_usage.get(name, 0),
            "total_requests": row.get("total_requests", 0),
            "successful": row.get("successful", 0),
            "failed": row.get("failed", 0),
            "grants_extracted": row.get("grants_extracted", 0),
        })

    # Add configured providers that haven't been used yet
    for name, cfg in provider_config.items():
        if name not in seen:
            providers.append({
                "provider_name": name,
                "model_name": cfg.get("model_name", ""),
                "active": cfg.get("active", False),
                "daily_limit": cfg.get("daily_limit", 100000),
                "used": 0,
                "total_requests": 0,
                "successful": 0,
                "failed": 0,
                "grants_extracted": 0,
            })

    # ── Daily breakdown (for chart) ───────────────────────────────────────────
    daily_sql = f"""
        SELECT
            DATE(creation) as date,
            COUNT(*) as total,
            SUM(CASE WHEN status = 'Success' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status != 'Success' THEN 1 ELSE 0 END) as failed
        FROM `tabLLM Usage Log`
        WHERE 1=1 {date_filter}
        GROUP BY DATE(creation)
        ORDER BY DATE(creation)
    """
    daily_breakdown = frappe.db.sql(daily_sql, as_dict=True)
    # Convert date objects to strings for JSON
    for row in daily_breakdown:
        row["date"] = str(row["date"])

    # ── Recent logs ───────────────────────────────────────────────────────────
    recent_logs = frappe.db.sql(f"""
        SELECT
            name, creation, provider_name, model_name,
            status, grants_extracted, content_chars_sent,
            page_url, response_time_ms
        FROM `tabLLM Usage Log`
        WHERE 1=1 {date_filter}
        ORDER BY creation DESC
        LIMIT 50
    """, as_dict=True)

    return {
        "totals": totals,
        "providers": providers,
        "daily_breakdown": daily_breakdown,
        "recent_logs": recent_logs,
    }


@frappe.whitelist()
def get_provider_credits():
    """Quick API to get today's remaining credits per provider.
    Used by Crawler Config to show live remaining_credits."""

    settings = None
    try:
        settings = frappe.get_single("Universal Crawler Settings")
    except Exception:
        return {}

    if not settings or not hasattr(settings, "llm_providers"):
        return {}

    # Only count actual API calls — exclude 'Rate Limited' which never hit the API
    today_usage_sql = """
        SELECT provider_name, COUNT(*) as used
        FROM `tabLLM Usage Log`
        WHERE DATE(creation) = CURDATE()
          AND status != 'Rate Limited'
        GROUP BY provider_name
    """
    today_usage = {r["provider_name"]: r["used"]
                   for r in frappe.db.sql(today_usage_sql, as_dict=True)}

    result = {}
    for row in settings.llm_providers:
        if row.active:
            limit = row.daily_limit or 100000
            used = today_usage.get(row.provider_name, 0)
            result[row.provider_name] = {
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
                "model": row.model_name,
            }

    return result

@frappe.whitelist(allow_guest=True)
def get_grants():
    return frappe.get_all(
        "Crawled Grant Record",
        fields=[
            "title",
            "organization",
            "funding_amount",
            "deadline",
            "source_url"
        ],
        order_by="creation desc",
        limit_page_length=20
    )
