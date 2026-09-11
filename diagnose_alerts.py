"""Read-only diagnostics; never prints credentials or sends Telegram messages."""
import json
import os
import sqlite3
import time
from projectbot.app import load_env
from projectbot.core import ROOT, matches
from projectbot.api import request, APIError, Freelancer
from projectbot.public_clients import fetch_client
import sys

load_env()
path = os.getenv("BOT_DB_PATH", str(ROOT / "data" / "bot.sqlite3"))
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
meta = {key: json.loads(value) for key, value in db.execute(
    "SELECT key,value FROM meta WHERE key IN ('config','last_scan','matched','fetched','telegram_cooldown_until','started','source_error','telegram_error','client_lookup_status')")}
cfg = meta.get("config", {})
print(json.dumps({"interval_seconds": cfg.get("interval_seconds"),
    "max_age_minutes": cfg.get("max_age_minutes"), "paused": cfg.get("paused"),
    "started": meta.get("started"), "last_scan_age_seconds": round(time.time() - meta.get("last_scan", time.time())),
    "last_matches": meta.get("matched"), "cooldown_seconds": max(0, round(meta.get("telegram_cooldown_until", 0) - time.time())),
    "queue": dict(db.execute("SELECT state,count(*) FROM alerts GROUP BY state")),
    "oauth_configured": bool(os.getenv("FREELANCER_OAUTH_TOKEN")),
    "fetched": meta.get("fetched"), "source_error": meta.get("source_error"),
    "telegram_error": meta.get("telegram_error"),
    "client_lookup_status": meta.get("client_lookup_status"),
    "filters": cfg,
    "last_sent_age_seconds": db.execute("SELECT CAST(? - MAX(sent) AS INTEGER) FROM alerts WHERE state='sent'", (time.time(),)).fetchone()[0]}, indent=2))
if "--offline" in sys.argv:
    row = db.execute("SELECT value FROM meta WHERE key='recent'").fetchone()
    recent = json.loads(row[0]) if row else []
    for p in recent[:20]:
        old = db.execute("SELECT state,sent FROM alerts WHERE id=?", (p["id"],)).fetchone()
        print(json.dumps({"id": p["id"], "title": p["title"], "created": p["created"],
            "age_seconds": round(time.time() - p["created"]) if p["created"] else None,
            "matches_now": matches(p, cfg), "previous_delivery": list(old) if old else None}))
if "--public" in sys.argv:
    recent_posts = db.execute("SELECT payload FROM post_views ORDER BY rendered_at DESC LIMIT 3").fetchall()
    for (payload,) in recent_posts:
        project = json.loads(payload)
        print(json.dumps({"project_id": project["id"], "url": project["url"],
                          "saved_client_states": project.get("verifications", {}),
                          "saved_client_avatar": project.get("client_avatar_url")}))
        try:
            print(json.dumps({"public_client_result": fetch_client(project)}))
        except APIError as e:
            print(str(e))
        if "--avatar" in sys.argv:
            try:
                details = Freelancer().client_details(project)
                print(json.dumps({"automatic_owner_lookup": {
                    "client_id_available": bool(details.get("client_id")),
                    "client_avatar_available": bool(details.get("client_avatar_url")),
                    "verification_count": len(details.get("verifications", {}))}}))
            except APIError as e:
                print(str(e))
            break
db.close()
if "--offline" in sys.argv:
    raise SystemExit(0)
headers = {"freelancer-oauth-v1": os.environ["FREELANCER_OAUTH_TOKEN"]} if os.getenv("FREELANCER_OAUTH_TOKEN") else {}
try:
    data = request("https://www.freelancer.com/api/projects/0.1/projects/active/?limit=3&user_details=true&user_status=true&user_country_details=true",
                   "Freelancer", headers=headers, timeout=15)
    result = data.get("result") or {}
    rows = result.get("projects") or []
    users = result.get("users") or {}
    print(json.dumps({"sample_projects": len(rows), "owners_returned": sum(p.get("owner_id") is not None for p in rows),
                      "client_profiles_returned": len(users),
                      "status_field_names": sorted({k for u in users.values() for k in (u.get("status") or {})})}, indent=2))
except APIError as e:
    print(str(e))
