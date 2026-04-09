import json
from datetime import date
from pathlib import Path

from . import config

class RateLimitTracker:
    """
    Tracks daily request counts for dynamic LLM providers.
    Persists state to disk so counts survive between script runs.
    Automatically resets at the start of a new day.
    """

    def __init__(self, providers: list, state_file: str = config.RATE_LIMIT_STATE_FILE):
        self.state_file = Path(state_file)
        self.providers = providers
        self.state = self._load()

    def _load(self) -> dict:
        today = str(date.today())
        default = {"date": today}
        for p in self.providers:
            name = p["provider_name"]
            # Sensible defaults for limits if unspecified
            limit = 100000 
            if "groq" in name.lower(): limit = 14400
            elif "gemini" in name.lower(): limit = 1500
            default[name] = {"used": 0, "limit": limit}

        if not self.state_file.exists():
            return default
        try:
            with open(self.state_file) as f:
                saved = json.load(f)
            # New day → reset counts
            if saved.get("date") != today:
                return default
                
            # Merge dynamically added providers to saved state
            for name, val in default.items():
                if name != "date" and name not in saved:
                    saved[name] = val
                    
            return saved
        except Exception:
            return default

    def _save(self) -> None:
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def can_use(self, provider: str) -> bool:
        p = self.state.get(provider, {})
        return p.get("used", 0) < p.get("limit", 999999)

    def increment(self, provider: str) -> None:
        if provider in self.state:
            self.state[provider]["used"] += 1
            self._save()

    def status_line(self) -> str:
        parts = []
        for name, data in self.state.items():
            if name != "date":
                parts.append(f"{name} {data['used']}/{data['limit']}")
        return "  |  ".join(parts) + f"  |  Date: {self.state['date']}"

    def print_status(self) -> None:
        print(f"\n  📊 Daily usage → {self.status_line()}")
