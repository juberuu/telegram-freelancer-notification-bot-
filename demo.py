"""Offline synthetic alert preview; no credentials or network needed."""
import json
import time
from projectbot.core import ROOT, alert, matches, normalize

raw = {"id": 1001, "title": "DEMO: Python AI chatbot integration", "description": "Build a FastAPI service with an LLM and React dashboard. This is a synthetic example, not a real project.", "status": "active", "type": "fixed", "time_submitted": time.time() - 180, "jobs": [{"name": "Python"}, {"name": "React"}], "budget": {"minimum": 250, "maximum": 750}, "currency": {"code": "USD"}, "owner_id": 7, "bid_stats": {"bid_count": 8}}
project = normalize(raw, {"7": {"location": {"country": {"name": "United States", "code": "US"}}, "status": {"payment_verified": True}}})
cfg = json.loads((ROOT / "config.example.json").read_text())
print("Matches default filters:", matches(project, cfg))
print(alert(project)[0])
