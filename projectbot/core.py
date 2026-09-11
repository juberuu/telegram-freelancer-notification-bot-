import html
import json
import math
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
VERIFICATION_FIELDS = {
    "identity_verified": "Identity", "payment_verified": "Payment",
    "deposit_made": "Deposit", "email_verified": "Email",
    "profile_complete": "Profile", "phone_verified": "Phone",
}
JST = timezone(timedelta(hours=9))


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def contains(text, term):
    return re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", text.casefold()) is not None


def avatar_url(value):
    """Accept real profile images hosted by Freelancer, including protocol-relative URLs."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    elif value.startswith("/"):
        value = "https://www.freelancer.com" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        trusted = host in ("freelancer.com", "f-cdn.com") or host.endswith((".freelancer.com", ".f-cdn.com"))
        if (parts.scheme not in ("http", "https") or not trusted or parts.username or parts.password
                or parts.port not in (None, 80, 443) or not parts.path):
            return None
        if any(marker in parts.path.lower() for marker in ("unknown", "default-avatar", "default_avatar", "/flags/")):
            return None
        return urlunsplit(("https", host, parts.path, parts.query, ""))
    except ValueError:
        return None


def client_avatar(client):
    # Only call with the project owner's record, never with bids or the whole page.
    for key in ("avatar_large_cdn", "avatarLargeCdn", "avatar_cdn", "avatarCdn",
                "avatar_large", "avatarLarge", "avatar", "profileLogoUrl"):
        value = client.get(key)
        if isinstance(value, dict):
            value = value.get("url")
        candidate = avatar_url(value)
        if candidate:
            return candidate
    return None


def client_id(value):
    text = str(value)
    return int(text) if text.isascii() and text.isdigit() and 0 < len(text) <= 18 and int(text) > 0 else None


def normalize(raw, users):
    owner_id = client_id(raw.get("owner_id"))
    owner = (users.get(str(owner_id)) or users.get(owner_id) or {}) if owner_id else {}
    if not owner and isinstance(raw.get("owner_info"), dict):
        inline = raw["owner_info"]
        inline_id = client_id(inline.get("id"))
        if inline_id and (owner_id is None or owner_id == inline_id):
            owner_id, owner = inline_id, inline
    status = owner.get("status") or {}
    verifications = {key: status[key] for key in VERIFICATION_FIELDS
                     if type(status.get(key)) is bool}
    country = (owner.get("location") or {}).get("country") or {}
    currency = raw.get("currency") or {}
    code = currency.get("code", "?")
    rate = 1.0 if code == "USD" else number(currency.get("exchange_rate"))
    budget = raw.get("budget") or {}
    maximum = number(budget.get("maximum"))
    seo = raw.get("seo_url")
    url = ("https://www.freelancer.com/projects/" + quote(seo.strip("/"), safe="/-")
           if seo else "https://www.freelancer.com/projects/" + str(int(raw["id"])))
    return {
        "id": int(raw["id"]), "title": str(raw.get("title") or "Untitled project"),
        "description": str(raw.get("description") or raw.get("preview_description") or ""),
        "skills": [j["name"] for j in raw.get("jobs") or [] if j.get("name")],
        "created": number(raw.get("time_submitted") or raw.get("submitdate")),
        "type": raw.get("type"), "currency": code,
        "minimum": number(budget.get("minimum")), "maximum": maximum,
        "max_usd": maximum * rate if maximum is not None and rate is not None and rate > 0 else None,
        "country": country.get("name") or "Unknown",
        "country_code": str(country.get("code") or "").upper(),
        "verified": verifications.get("payment_verified"),
        "verifications": verifications,
        "client_avatar_url": client_avatar(owner),
        "client_id": owner_id,
        "bids": number((raw.get("bid_stats") or {}).get("bid_count")),
        "url": url, "active": raw.get("status") == "active" and not raw.get("deleted"),
    }


def matches(p, cfg, now=None):
    now = time.time() if now is None else now
    if not p["active"] or p["created"] is None or not -60 <= now - p["created"] <= cfg["max_age_minutes"] * 60:
        return False
    text = " ".join([p["title"], p["description"], *p["skills"]])
    if cfg["keywords"] and not any(contains(text, k) for k in cfg["keywords"]):
        return False
    if any(contains(text, k) for k in cfg["exclude"]):
        return False
    if cfg["project_type"] != "any" and p["type"] != cfg["project_type"]:
        return False
    if p["type"] not in ("fixed", "hourly"):
        return False
    threshold = cfg["min_hourly_usd"] if p["type"] == "hourly" else cfg["min_fixed_usd"]
    if threshold > 0 and (p["max_usd"] is None or p["max_usd"] < threshold):
        return False
    if cfg["countries"] and p["country_code"] not in cfg["countries"]:
        return False
    if cfg["verified_only"] and p["verified"] is not True:
        return False
    if cfg["max_bids"] is not None and (p["bids"] is None or p["bids"] > cfg["max_bids"]):
        return False
    return True


def clip_text(value, limit):
    """Truncate before HTML escaping, preferably at a word boundary."""
    value = str(value).strip()
    if len(value) <= limit:
        return value
    head = value[:limit - 1]
    boundary = max(head.rfind(" "), head.rfind("\n"))
    if boundary >= limit // 2:
        head = head[:boundary]
    return head.rstrip() + "…"


def _alert_sections(p, now=None, preview=False, expanded=False):
    esc = lambda s: html.escape(str(s))
    fmt = lambda v: f"{v:g}" if v is not None else "?"
    hourly = p["type"] == "hourly"
    unit = " / hour" if hourly else ""
    kind = "Hourly" if hourly else "Fixed"
    if p["created"] is None:
        posted = ""
    else:
        posted = datetime.fromtimestamp(p["created"], JST).strftime("%Y-%m-%d %H:%M:%S")
    # Old cached alerts contain only the payment flag.
    verifications = {"payment_verified": p.get("verified"), **(p.get("verifications") or {})}
    verification_lines = []
    for key, label in VERIFICATION_FIELDS.items():
        value = verifications.get(key)
        if type(value) is bool:
            label = "ID" if key == "identity_verified" else label
            verification_lines.append(f"{'✅' if value else '⚠️'} {label}")
    sections = [("<i>Preview</i>\n" if preview else "")
                + f'<b><a href="{esc(p["url"])}">{esc(clip_text(p["title"], 180))}</a></b>']
    amounts = [p["minimum"], p["maximum"]]
    if any(v is not None for v in amounts) and p["currency"] not in (None, "", "?", "Unknown"):
        if all(v is not None for v in amounts):
            budget = f"{fmt(amounts[0])}–{fmt(amounts[1])}"
        else:
            budget = f"From {fmt(amounts[0])}" if amounts[0] is not None else f"Up to {fmt(amounts[1])}"
        sections.append(f"💰 <b>{esc(budget + ' ' + p['currency'] + unit)}</b>"
                        + (f" · {kind}" if p["type"] in ("fixed", "hourly") else ""))
    activity = [sections.pop()] if len(sections) > 1 else []
    if posted:
        activity.append(f"🕒 {posted}")
    metadata_rows = [" · ".join(activity)] if activity else []
    activity = []
    code = str(p.get("country_code") or "").strip().upper()
    country = str(p.get("country") or "").strip()
    if country.casefold() in ("unknown", "?", "n/a"):
        country = ""
    flag = ""
    if len(code) == 2 and code.isascii() and code.isalpha():
        flag = "".join(chr(127397 + ord(c)) for c in code)
    if flag or country:
        activity.append(" ".join(part for part in (flag, esc(clip_text(country, 80))) if part))
    if p["bids"] is not None:
        activity.append(f"👥 {fmt(p['bids'])} bids")
    if activity:
        metadata_rows.append(" · ".join(activity))
    if metadata_rows:
        sections.append("\n".join(metadata_rows))
    if verification_lines:
        sections.append("🛡 <b>Client</b>\n" + " · ".join(verification_lines))
    skills = list(dict.fromkeys(str(s).strip() for s in p["skills"] if s and str(s).strip() and str(s).strip().casefold() not in ("unknown", "?", "n/a")))
    if skills:
        shown = " · ".join(esc(skill) for skill in skills)
        sections.append(f"🏷 <b>Skills</b>\n{shown}")
    return sections, {"inline_keyboard": []}


def alert(p, now=None, preview=False, expanded=False):
    sections, markup = _alert_sections(p, now, preview, expanded)
    return "\n\n".join(sections), markup


def rich_alert(p, now=None, preview=False):
    """Telegram native rich content with a linked title and compact metadata."""
    sections, _ = _alert_sections(p, now, preview)
    blocks = []
    avatar = avatar_url(p.get("client_avatar_url"))
    if avatar:
        blocks.append('<figure><img src="tg://photo?id=client_avatar"/><figcaption>Client</figcaption></figure>')
    if preview:
        blocks.append("<p><i>Preview</i></p>")
    title = sections[0].removeprefix("<i>Preview</i>\n")
    blocks.append("<h2>" + title.removeprefix("<b>").removesuffix("</b>") + "</h2>")
    # Only skills use the user-selected native shaded quote treatment.
    for group in sections[1:]:
        blocks.append("<hr/>")
        body = group.replace("\n", "<br>")
        tag = "blockquote" if group.startswith("🏷 <b>Skills</b>") else "p"
        blocks.append(f"<{tag}>" + body + f"</{tag}>")
    result = {"html": "".join(blocks), "skip_entity_detection": True}
    if avatar:
        result["media"] = [{"id": "client_avatar", "media": {"type": "photo", "media": avatar}}]
    return result


class Store:
    def __init__(self, path):
        self.path = Path(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                created REAL NOT NULL, sent REAL
            );
            CREATE TABLE IF NOT EXISTS post_views (
                message_id INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                preview INTEGER NOT NULL, rendered_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_history (
                id INTEGER PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS history_created ON project_history(created);
        """)
        if self.get("config") is None:
            self.set("config", json.loads((ROOT / "config.example.json").read_text()))

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def enqueue(self, projects, repeat=False):
        with self.lock, self.db:
            for p in projects:
                # Each scan may explicitly requeue sent projects. Retries do not requeue them.
                self.db.execute("""INSERT INTO alerts(id,payload,created) VALUES (?,?,?)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,
                    created=excluded.created,
                    state=CASE WHEN ? OR alerts.state='skipped' THEN 'pending' ELSE alerts.state END""",
                    (p["id"], json.dumps(p), p["created"], repeat))

    def pending(self, limit=10):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM alerts WHERE state='pending' ORDER BY created DESC LIMIT ?", (limit,))]

    def save_post(self, message_id, project, preview, rendered_at):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO post_views VALUES (?,?,?,?)",
                            (message_id, json.dumps(project), int(preview), rendered_at))

    def post(self, message_id):
        with self.lock:
            row = self.db.execute("SELECT payload,preview,rendered_at FROM post_views WHERE message_id=?",
                                  (message_id,)).fetchone()
            return (json.loads(row[0]), bool(row[1]), row[2]) if row else None

    def unfinished_client_posts(self, limit=100):
        """Recover recent enrichment work after a restart without replaying alerts."""
        with self.lock:
            rows = self.db.execute("SELECT message_id,payload FROM post_views WHERE rendered_at>=? ORDER BY rendered_at DESC LIMIT ?",
                                   (time.time() - 86400, limit)).fetchall()
        result = []
        for message_id, payload in rows:
            p = json.loads(payload)
            if p.get("_client_enrichment_done"):
                continue
            states = {"payment_verified": p.get("verified"), **p.get("verifications", {})}
            if not avatar_url(p.get("client_avatar_url")) or any(type(states.get(key)) is not bool for key in VERIFICATION_FIELDS):
                result.append((-(p.get("created") or 0), message_id, 0))
        return result

    def mark(self, pid, state):
        with self.lock, self.db:
            self.db.execute("UPDATE alerts SET state=?,sent=? WHERE id=?", (state, time.time() if state == "sent" else None, pid))

    def counts(self):
        with self.lock:
            return dict(self.db.execute("SELECT state,COUNT(*) FROM alerts GROUP BY state"))

    def match_counts(self, projects):
        """Current scan's matches only, separate from the full delivery history."""
        counts = {"pending": 0, "sent": 0, "skipped": 0, "unseen": 0}
        with self.lock:
            for pid in {p["id"] for p in projects}:
                row = self.db.execute("SELECT state FROM alerts WHERE id=?", (pid,)).fetchone()
                state = row[0] if row else "unseen"
                counts[state] = counts.get(state, 0) + 1
        return counts

    def record_history(self, projects, now=None):
        now = time.time() if now is None else now
        with self.lock, self.db:
            self.db.execute("INSERT INTO meta VALUES ('stats_tracking_since',?) ON CONFLICT(key) DO NOTHING",
                            (json.dumps(now),))
            for p in projects:
                created = number(p.get("created"))
                if created is None or created > now + 60:
                    continue
                # An edited/relisted timestamp must not turn the same ID into a new project.
                self.db.execute("""INSERT INTO project_history VALUES (?,?,?)
                    ON CONFLICT(id) DO UPDATE SET created=MIN(project_history.created,excluded.created),
                    payload=excluded.payload""", (p["id"], created, json.dumps(p)))
            self.db.execute("INSERT INTO meta VALUES ('stats_last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(now),))
            self.db.execute("DELETE FROM project_history WHERE created < ?", (now - 30 * 86400,))

    def history(self, since, until):
        with self.lock:
            rows = self.db.execute("SELECT created,payload FROM project_history WHERE created>=? AND created<=?",
                                   (since, until)).fetchall()
        projects = []
        for created, payload in rows:
            p = json.loads(payload)
            p["created"] = created
            projects.append(p)
        return projects

    def prune(self):
        with self.lock, self.db:
            self.db.execute("DELETE FROM alerts WHERE created < ?", (time.time() - 30 * 86400,))
            self.db.execute("DELETE FROM post_views WHERE rendered_at < ?", (time.time() - 30 * 86400,))
