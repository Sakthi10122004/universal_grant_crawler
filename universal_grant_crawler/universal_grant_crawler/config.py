"""
config.py — Edit this file to set your API keys and preferences.

Free API keys:
  Groq   → https://console.groq.com          (14,400 req/day free)
  Gemini → https://aistudio.google.com/app/apikey  (1,500 req/day free)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ──────────────────────────────────────────────────────────────────
# Set keys here OR export as environment variables (env vars take priority)

GROQ_API_KEY   = os.environ.get("GROQ_API_KEY")   # or: export GROQ_API_KEY=your_key
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")   # or: export GEMINI_API_KEY=your_key

# ── Model Selection ───────────────────────────────────────────────────────────
GROQ_MODEL   = "llama-3.3-70b-versatile"      # Best accuracy on Groq free tier
GEMINI_MODEL = "gemini-2.5-flash"     # Best accuracy on Gemini free tier

# ── Daily Rate Limits (free tier) ────────────────────────────────────────────
GROQ_DAILY_LIMIT   = 14_400
GEMINI_DAILY_LIMIT = 1_500

# ── Scraper Settings ──────────────────────────────────────────────────────────
PAGE_LOAD_WAIT_SECONDS = 4      # Extra wait for JS-heavy pages
REQUEST_TIMEOUT        = 30     # Playwright page load timeout (seconds)
MAX_CONTENT_CHARS      = 12_000 # Max page text sent to LLM (fits in context)
RETRY_ATTEMPTS         = 2      # Retry failed extractions
RETRY_DELAY_SECONDS    = 3      # Wait between retries

# ── Crawler Settings ──────────────────────────────────────────────────────────
MAX_PAGINATION_PAGES   = 50     # Max pages to follow via Next button
MAX_SCROLL_ATTEMPTS    = 30     # Max scroll steps for infinite scroll pages
MAX_LOAD_MORE_CLICKS   = 20     # Max Load More button clicks per page

# ── Output ────────────────────────────────────────────────────────────────────
DEFAULT_OUTPUT_FILE    = "grants.json"
RATE_LIMIT_STATE_FILE  = ".rate_limit_state.json"  # tracks daily usage
