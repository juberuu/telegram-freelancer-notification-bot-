import html
import hashlib
import json
import logging
import os
import queue
import signal
import threading
import time
from datetime import datetime, timezone
from .api import APIError, Freelancer, Telegram
from .core import ROOT, Store, alert, rich_alert, matches, number, VERIFICATION_FIELDS, avatar_url
from .public_clients import fetch_client, enrich
from .statistics import project_counts, render_counts

HELP = """<b>Project alerts · Freelancer.com</b>
/filters — current settings
/keywords python, react, AI — match ANY term
/skills Python, Graphic Design — set your own skills (same as /keywords)
/exclude wordpress, data entry — exclude ANY term
/budget 100 — minimum fixed upper budget, USD
/hourly 15 — minimum hourly upper rate, USD/hour
/type any — any, fixed, or hourly
/countries US, GB, AU — client country codes
/verified on — require verified client payment
/maxbids 30 — maximum current bids
/age 5 — only projects from the last 5 minutes
/interval 10 — check every 10 seconds
/check — queue a fresh scan
/latest — preview up to 5 cached matches (may repeat)
/stats — project counts by category: 30m, 1h, 1d, 3d, 1w
/stats on · /stats off — enable or disable 30-minute summaries
/pause · /resume · /stop · /status · /help

Use “off” to clear keywords, exclusions, countries, or maxbids.
Your filters affect only your own alerts. /stop unsubscribes; /start subscribes again.
/verified off disables the payment requirement.
Non-USD budgets use Freelancer's approximate exchange rate.
Country/payment data may require authorized API access."""

MENU = {"keyboard": [["/filters", "/status"], ["/check", "/latest"], ["/stats"], ["/pause", "/resume"]], "resize_keyboard": True}

# Client verification is the most important field, so a live alert tries to include it in the
# first message instead of a later edit. This bounds how long a send waits for the public-page
# lookup before it goes out anyway and the background workers finish enrichment.
CLIENT_LOOKUP_TIMEOUT = 6


def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def change_filter(cfg, command, arg):
    cfg = dict(cfg)
    if command in ("keywords", "exclude", "countries"):
        values = [] if arg.lower() == "off" else list(dict.fromkeys(s.strip() for s in arg.split(",") if s.strip()))
        if not arg or len(values) > 30 or any(len(s) > 80 for s in values):
            raise ValueError("Enter up to 30 comma-separated values, or off.")
        if command == "countries":
            values = [s.upper() for s in values]
            if any(len(s) != 2 or not s.isascii() or not s.isalpha() for s in values):
                raise ValueError("Use two-letter client country codes, e.g. US, GB, AU; or off.")
        cfg[command] = values
    elif command in ("budget", "hourly", "age", "interval", "maxbids"):
        key, low, high = {
            "budget": ("min_fixed_usd", 0, 1e9), "hourly": ("min_hourly_usd", 0, 1e6),
            "age": ("max_age_minutes", 5, 1440), "interval": ("interval_seconds", 10, 3600),
            "maxbids": ("max_bids", 0, 100000),
        }[command]
        value = number(arg)
        if command == "maxbids" and arg.lower() == "off":
            value = None
        elif value is None or not low <= value <= high or (command in ("age", "interval", "maxbids") and value != int(value)):
            raise ValueError(f"/{command} needs a {'whole ' if command in ('age', 'interval', 'maxbids') else ''}number from {low:g} to {high:g}.")
        cfg[key] = int(value) if value is not None and command in ("age", "interval", "maxbids") else value
    elif command == "type":
        if arg.lower() not in ("any", "fixed", "hourly"):
            raise ValueError("Use /type any, /type fixed, or /type hourly.")
        cfg["project_type"] = arg.lower()
    elif command == "verified":
        if arg.lower() not in ("on", "off"):
            raise ValueError("Use /verified on or /verified off.")
        cfg["verified_only"] = arg.lower() == "on"
    else:
        raise ValueError("Unknown command. Use /help.")
    return cfg


def filters_text(cfg):
    return "<b>Your filters</b>\n<pre>" + html.escape(json.dumps(cfg, indent=2)) + "</pre>\nUse /help for editing commands. Budget floors compare the project's upper budget, not guaranteed payment."


class Bot:
    def __init__(self, store, telegram, source):
        self.store, self.tg, self.source = store, telegram, source
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.delivery_wake = threading.Event()
        self.client_queue = queue.PriorityQueue(maxsize=2000)
        self.avatar_queue = queue.PriorityQueue(maxsize=2000)
        self.client_cache = {}
        self.client_locks = [threading.Lock() for _ in range(64)]
        self.client_state = {"retry_until": 0}
        self.stats_store = store
        self.stats_lock = threading.Lock()
        if self.store.get("stats_next_at") is None:
            self.store.set("stats_next_at", time.time() + 1800)
        # One-time upgrade: prioritize fresh alerts without overriding later choices.
        if not self.store.get("fresh_alerts_v1"):
            cfg = self.store.get("config")
            cfg["interval_seconds"] = min(cfg["interval_seconds"], 30)
            self.store.set("config", cfg)
            self.store.set("fresh_alerts_v1", True)
        if not self.store.get("fresh_alerts_v2"):
            cfg = self.store.get("config")
            cfg["max_age_minutes"] = 5
            cfg["interval_seconds"] = 30
            self.store.set("config", cfg)
            self.store.set("fresh_alerts_v2", True)
        if not self.store.get("fresh_alerts_v3"):
            cfg = self.store.get("config")
            cfg["interval_seconds"] = 10
            self.store.set("config", cfg)
            self.store.set("fresh_alerts_v3", True)
        self.public_retry_until = 0
        for job in self.store.unfinished_client_posts():
            self.client_queue.put_nowait(job)

    @property
    def public_retry_until(self):
        return self.client_state["retry_until"]

    @public_retry_until.setter
    def public_retry_until(self, value):
        self.client_state["retry_until"] = value

    def authorized(self, message):
        return (message.get("chat", {}).get("type") == "private"
                and message.get("chat", {}).get("id") == self.tg.chat_id
                and message.get("from", {}).get("id") == self.tg.chat_id)

    def handle(self, update):
        if update.get("callback_query"):
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        if not self.authorized(message) or not isinstance(message.get("text"), str):
            return
        parts = message["text"].strip().split(maxsplit=1)
        if not parts:
            return
        command = parts[0].split("@")[0].lstrip("/").lower()
        if command == "skills":
            command = "keywords"
        arg = parts[1].strip() if len(parts) > 1 else ""
        cfg = self.store.get("config")
        if command in ("help", "start"):
            if command == "start":
                cfg["paused"] = False
                self.store.set("config", cfg)
                self.store.set("started", True)
                self.store.set("stats_next_at", time.time() + 1800)
                self.wake.set()
            self.tg.send(("<b>Subscribed.</b> Choose your skills with /skills Python, React, Design.\n\n"
                          if command == "start" else "") + HELP, MENU)
        elif command == "filters":
            self.tg.send(filters_text(cfg))
        elif command == "stats":
            if arg.lower() in ("on", "off"):
                enabled = arg.lower() == "on"
                self.store.set("stats_auto", enabled)
                if enabled:
                    self.store.set("stats_next_at", time.time() + 1800)
                self.tg.send("Project-count summaries enabled every 30 minutes. Use /stats to view them now." if enabled
                             else "Automatic project-count summaries disabled. /stats is still available.")
            elif arg:
                self.tg.send("Use /stats to view counts, /stats on, or /stats off.")
            else:
                self.send_stats()
        elif command == "status":
            status = "paused" if cfg["paused"] else ("running" if self.store.get("started") else "waiting for /start")
            checked = self.store.get("last_scan")
            stamp = datetime.fromtimestamp(checked, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if checked else "not yet"
            stats = self.store.counts()
            text = f"Status: {status}\nLast successful scan: {stamp}\nFetched: {self.store.get('fetched', 0)} · Matches: {self.store.get('matched', 0)}\nSent (30-day history): {stats.get('sent', 0)} · Pending: {stats.get('pending', 0)}"
            current = [p for p in self.store.get("recent", []) if matches(p, cfg)]
            current_counts = self.store.match_counts(current)
            text += f"\nEligible now: {len(current)} · New/pending: {current_counts['pending'] + current_counts['unseen']} · Already sent: {current_counts['sent']}"
            if not current:
                text += "\nWaiting for new projects that meet your filters."
            elif current_counts["sent"] == len(current):
                text += "\nCurrent matches were already delivered. /latest shows previews."
            text += f"\nScan interval: {cfg['interval_seconds']}s · New projects only · Max age: {cfg['max_age_minutes']} min"
            if self.store.get("client_lookup_status"):
                text += "\nClient details: " + self.store.get("client_lookup_status")
            if self.store.get("last_delivery_age") is not None:
                text += f"\nLast delivered project's age: {self.store.get('last_delivery_age')}s"
            if self.store.get("last_skip_reason"):
                text += "\nLast skipped alert: " + self.store.get("last_skip_reason")
            for key in ("source_error", "telegram_error"):
                if self.store.get(key):
                    text += "\n" + self.store.get(key)
            text += "\n" + "\n".join(self.store.get("warnings", []))
            self.tg.send(html.escape(text))
        elif command in ("pause", "resume", "stop"):
            cfg["paused"] = command != "resume"
            self.store.set("config", cfg)
            if command == "stop":
                self.store.set("started", False)
            if command == "resume":
                self.store.set("started", True)
                self.store.set("stats_next_at", time.time() + 1800)
            self.wake.set()
            self.tg.send("Unsubscribed. Your filters are saved; /start subscribes again." if command == "stop"
                         else "Alerts paused." if cfg["paused"] else "Alerts resumed. Scanning shortly.")
        elif command == "check":
            if cfg["paused"]:
                self.tg.send("Alerts are paused. Use /resume first.")
            else:
                self.store.set("started", True)
                self.wake.set()
                self.tg.send("Scan requested. Matching projects will arrive automatically; use /status to see the result.")
        elif command == "latest":
            projects = [p for p in self.store.get("recent", []) if matches(p, cfg)][:5]
            if not projects:
                self.tg.send("No matching projects in the last successful scan. Try /check, then /status, or loosen /filters.")
            for p in projects:
                self.send_post(p, preview=True)
                self.stop.wait(1.1)
        else:
            try:
                new_cfg = change_filter(cfg, command, arg)
            except ValueError as e:
                self.tg.send(html.escape(str(e)))
                return
            self.store.set("config", new_cfg)
            self.wake.set()
            self.tg.send("Setting saved.\n" + filters_text(new_cfg))

    def send_stats(self, automatic=False, message_id=None):
        with self.stats_lock:
            now = time.time()
            if automatic and (not self.store.get("started") or self.store.get("config")["paused"]
                              or not self.store.get("stats_auto", True)
                              or now < self.store.get("stats_next_at", now + 1800)
                              or now < self.store.get("stats_retry_at", 0)):
                return False
            text, rich = render_counts(project_counts(self.stats_store, self.store.get("config"), now=now))
            markup = {"inline_keyboard": [[{"text": "🔄 Refresh counts", "callback_data": "stats:refresh"}]]}
            fingerprint = hashlib.sha256(json.dumps([text, rich], sort_keys=True).encode()).hexdigest()
            views = self.store.get("stats_message_views", {})
            if message_id is not None and views.get(str(message_id)) == fingerprint:
                return False
            if message_id is None:
                result = self.tg.send(text, markup, rich_message=rich)
                if isinstance(result, dict):
                    message_id = result.get("message_id")
            else:
                self.tg.edit(message_id, text, markup, rich_message=rich)
            if message_id is not None:
                views[str(message_id)] = fingerprint
                self.store.set("stats_message_views", dict(list(views.items())[-20:]))
            if automatic:
                self.store.set("stats_next_at", time.time() + 1800)
            self.store.set("stats_retry_at", 0)
            return True

    def send_post(self, project, preview=False):
        if not avatar_url(project.get("client_avatar_url")) or any(type(project.get("verifications", {}).get(key)) is not bool for key in VERIFICATION_FIELDS):
            cached = self.client_cache.get(project["id"])
            if cached and cached[0] > time.monotonic():
                project = enrich(project, cached[1])
            else:
                # Fetch client verification before the first send so it arrives with the alert
                # rather than in a later edit. A slow, unavailable, or rate-limited public page
                # still lets the alert go out now; background workers finish the lookup.
                try:
                    project = enrich(project, self.client_details(project, timeout=CLIENT_LOOKUP_TIMEOUT))
                except APIError as e:
                    logging.warning("%s; client states will be retried in the background", e)
        cfg = self.store.get("config")
        if not preview and (cfg["paused"] or not matches(project, cfg)):
            return False
        rendered_at = time.time()
        text, markup = alert(project, now=rendered_at, preview=preview)
        avatar = avatar_url(project.get("client_avatar_url"))
        result = self.tg.send(text, markup, rich_message=rich_alert(project, now=rendered_at, preview=preview),
                              **({"avatar_url": avatar} if avatar else {}))
        if isinstance(result, dict) and "message_id" in result:
            project = {**project, "_render_mode": result.get("_render_mode", "text")}
            self.store.save_post(result["message_id"], project, preview, rendered_at)
            if not avatar or any(type(project.get("verifications", {}).get(key)) is not bool for key in VERIFICATION_FIELDS):
                try:
                    self.client_queue.put_nowait((-(project.get("created") or 0), result["message_id"], 0))
                except queue.Full:
                    logging.warning("Public client lookup queue is full; project alert was still delivered")
        return True

    def client_details(self, project, timeout=15):
        # Share one in-flight public request per project across all subscribers.
        with self.client_locks[hash(project["id"]) % len(self.client_locks)]:
            return self._client_details(project, timeout)

    def _client_details(self, project, timeout=15):
        cached = self.client_cache.get(project["id"])
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if time.monotonic() < self.public_retry_until:
            raise APIError("Freelancer public page cooldown", 429, int(self.public_retry_until - time.monotonic()) + 1)
        try:
            details = fetch_client(project, timeout=timeout)
        except APIError as e:
            if e.code == 429:
                self.public_retry_until = time.monotonic() + e.retry_after
            self.store.set("client_lookup_status", str(e))
            raise
        if len(self.client_cache) >= 200:
            self.client_cache.pop(next(iter(self.client_cache)), None)
        self.client_cache[project["id"]] = (time.monotonic() + (60 if details.get("verifications") else 30), details)
        count = len(details.get("verifications", {}))
        has_avatar = bool(avatar_url(details.get("client_avatar_url")))
        self.store.set("client_lookup_status", f"Public page supplied {count}/6 states; avatar {'available' if has_avatar else 'not supplied'} for project {project['id']}")
        return details

    def enrich_post(self, message_id, public_only=False, avatar_only=False):
        saved = self.store.post(message_id)
        if not saved:
            return
        project, preview, rendered_at = saved
        lookup_error = None
        try:
            details = {} if avatar_only else self.client_details(project, timeout=6)
        except APIError as e:
            details, lookup_error = {}, e
        updated = enrich(project, details, prefer_new=True)
        # Publish verification/country data as soon as the public page provides it.
        # A slower owner/avatar API request must not hold these states back.
        self.update_client_post(message_id, project, updated, preview, rendered_at)
        if public_only:
            if lookup_error:
                raise lookup_error
            return
        project = updated
        if self.source and (not updated.get("client_avatar_url") or any(
                type(updated.get("verifications", {}).get(key)) is not bool for key in VERIFICATION_FIELDS)):
            try:
                updated = enrich(updated, self.source.client_details(updated), prefer_new=True)
            except APIError as e:
                lookup_error = e
        has_avatar = bool(avatar_url(updated.get("client_avatar_url")))
        self.store.set("client_lookup_status", f"Project {project['id']}: "
                       f"{len(updated.get('verifications', {}))}/6 states; "
                       + ("client avatar available" if has_avatar else "client avatar not supplied by Freelancer")
                       + (f"; {lookup_error}" if lookup_error else ""))
        updated = {**updated, "_client_enrichment_done": lookup_error is None}
        self.update_client_post(message_id, project, updated, preview, rendered_at)
        if lookup_error:
            raise lookup_error

    def update_client_post(self, message_id, project, updated, preview, rendered_at):
        text, markup = alert(updated, now=rendered_at, preview=preview)
        if (text == alert(project, now=rendered_at, preview=preview)[0]
                and avatar_url(updated.get("client_avatar_url")) == avatar_url(project.get("client_avatar_url"))):
            self.store.save_post(message_id, updated, preview, rendered_at)
            return
        avatar = avatar_url(updated.get("client_avatar_url"))
        self.tg.edit(message_id, text, markup, rich_message=rich_alert(updated, now=rendered_at, preview=preview),
                     **({"avatar_url": avatar} if avatar else {}))
        self.store.save_post(message_id, updated, preview, rendered_at)
        logging.info("Client details updated for project %s", project["id"])

    def client_worker(self):
        while not self.stop.is_set():
            try:
                job = self.client_queue.get(timeout=1)
            except queue.Empty:
                continue
            priority, message_id, attempts = job
            try:
                self.enrich_post(message_id)
            except APIError as e:
                logging.warning("%s; original project alert remains available", e)
                if attempts < 2 and (e.code is None or e.code == 429 or e.code >= 500):
                    if self.stop.wait(e.retry_after):
                        return
                    try:
                        self.client_queue.put_nowait((priority, message_id, attempts + 1))
                    except queue.Full:
                        pass
            finally:
                self.client_queue.task_done()
            # Public-page requests run separately and at a modest rate.
            if self.stop.wait(3):
                return

    def handle_callback(self, query):
        message = query.get("message") or {}
        if not self.authorized({"chat": message.get("chat", {}), "from": query.get("from", {})}):
            return
        data = query.get("data")
        if data in ("price:fixed", "price:hourly"):
            self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                         "text": "Fixed Price" if data == "price:fixed" else "Hourly Price",
                         "show_alert": True})
            return
        if data == "stats:refresh":
            self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"]})
            self.send_stats(message_id=message["message_id"])
            return
        if data not in ("skills:more", "skills:less"):
            return
        saved = self.store.post(message.get("message_id"))
        if not saved:
            self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"],
                         "text": "This preview has expired. Use /latest for a new post."})
            return
        self.tg.call("answerCallbackQuery", {"callback_query_id": query["id"]})
        project, preview, rendered_at = saved
        text, markup = alert(project, now=rendered_at, preview=preview, expanded=data == "skills:more")
        # Ignore repeated taps on the already-selected view.
        if message.get("reply_markup") == markup:
            return
        self.tg.call("editMessageText", {"chat_id": self.tg.chat_id,
                     "message_id": message["message_id"], "text": text, "parse_mode": "HTML",
                     "link_preview_options": Telegram.preview_options(project.get("client_avatar_url")), "reply_markup": markup})

    def scan(self):
        cfg = self.store.get("config")
        projects, warnings = self.source.fetch(cfg["max_age_minutes"])
        self.stats_store.record_history(projects)
        # Re-read settings in case a command arrived during the HTTP request.
        cfg = self.store.get("config")
        matched = [p for p in projects if matches(p, cfg)]
        self.store.enqueue(matched)
        self.delivery_wake.set()
        self.store.set("recent", projects)
        self.store.set("last_scan", time.time())
        self.store.set("fetched", len(projects))
        self.store.set("matched", len(matched))
        self.store.set("warnings", warnings)
        self.store.set("source_error", None)
        self.store.prune()
        counts = self.store.match_counts(matched)
        logging.info("Scan complete: %d projects, %d matches: %d new/pending, %d already sent",
                     len(projects), len(matched), counts["pending"], counts["sent"])

    def deliver(self, max_posts=None):
        delivered = 0
        while not self.stop.is_set():
            # Refresh the selection after every post so new arrivals jump ahead of older work.
            pending = self.store.pending(limit=1)
            if not pending:
                break
            p = pending[0]
            cfg = self.store.get("config")
            if cfg["paused"] or self.stop.is_set():
                return
            if not matches(p, cfg):
                age = max(0, int(time.time() - p["created"])) if p.get("created") else None
                reason = (f"Expired before delivery ({age}s old)" if age is not None and age > cfg["max_age_minutes"] * 60
                          else "No longer matches your filters")
                self.store.set("last_skip_reason", f"Project {p['id']}: {reason}")
                logging.info("Project %s for subscriber %s: %s", p["id"], self.tg.chat_id, reason)
                self.store.mark(p["id"], "skipped")
                continue
            if self.send_post(p) is False:
                self.store.mark(p["id"], "skipped")
                continue
            self.store.mark(p["id"], "sent")
            delivered += 1
            self.store.set("telegram_error", None)
            self.store.set("last_delivery_age", max(0, int(time.time() - p["created"])))
            if max_posts is not None and delivered >= max_posts:
                break
            if self.stop.wait(1.1):
                return
        if delivered:
            logging.info("Delivery complete: %d project posts sent", delivered)

    def worker(self):
        next_scan = 0
        source_retry_at = 0
        failures = 0
        while not self.stop.is_set():
            requested = self.wake.is_set()
            self.wake.clear()
            cfg = self.store.get("config")
            if self.store.get("started") and not cfg["paused"]:
                now = time.monotonic()
                if requested:
                    next_scan = 0
                if now >= source_retry_at and now >= next_scan:
                    try:
                        self.scan()
                        failures = 0
                        next_scan = now + self.store.get("config")["interval_seconds"]
                    except APIError as e:
                        failures += 1
                        source_retry_at = time.monotonic() + max(e.retry_after, min(60, 5 * 2 ** min(failures - 1, 4)))
                        next_scan = source_retry_at
                        self.store.set("source_error", str(e))
                        logging.warning("%s; will retry", e)
            self.wake.wait(1)

    def delivery_worker(self):
        retry_at = 0
        while not self.stop.is_set():
            self.delivery_wake.clear()
            cfg = self.store.get("config")
            if self.store.get("started") and not cfg["paused"] and time.monotonic() >= retry_at:
                try:
                    self.deliver()
                    self.send_stats(automatic=True)
                except APIError as e:
                    self.store.set("telegram_error", str(e))
                    retry_at = time.monotonic() + max(5, e.retry_after)
                    logging.warning("%s; alert remains pending; source scanning continues", e)
            self.delivery_wake.wait(1)

    def run(self):
        while not self.stop.is_set():
            try:
                self.tg.call("getMe")
                webhook = self.tg.call("getWebhookInfo")
                break
            except APIError as e:
                if e.code in (401, 403, 404):
                    raise
                logging.warning("%s; connection setup will retry automatically", e)
                if self.stop.wait(e.retry_after):
                    return
        else:
            return
        if webhook.get("url"):
            raise RuntimeError("This bot has an existing webhook. Use a dedicated new bot or remove that webhook yourself before running.")
        thread = threading.Thread(target=self.worker, name="project-poller", daemon=True)
        delivery_thread = threading.Thread(target=self.delivery_worker, name="project-delivery", daemon=True)
        client_thread = threading.Thread(target=self.client_worker, name="client-details", daemon=True)
        thread.start()
        delivery_thread.start()
        client_thread.start()
        logging.info("Bot connected. Send /start in your private bot chat.")
        try:
            while not self.stop.is_set():
                if not thread.is_alive() or not delivery_thread.is_alive() or not client_thread.is_alive():
                    raise RuntimeError("Project worker stopped. Restart the service and inspect its logs.")
                try:
                    updates = self.tg.call("getUpdates", {"offset": self.store.get("offset", 0), "timeout": 20, "allowed_updates": ["message", "callback_query"]})
                    for update in updates:
                        self.handle(update)
                        self.store.set("offset", update["update_id"] + 1)
                except APIError as e:
                    logging.warning("%s; polling will retry", e)
                    self.stop.wait(max(5, e.retry_after))
        finally:
            self.stop.set()
            self.wake.set()
            self.delivery_wake.set()
            thread.join(timeout=30)
            delivery_thread.join(timeout=30)
            client_thread.join(timeout=30)


def main():
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    owner = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not owner.isdigit() or int(owner) <= 0:
        raise SystemExit("Run python setup_bot.py first, or set TELEGRAM_BOT_TOKEN and a positive private TELEGRAM_CHAT_ID.")
    store = Store(os.getenv("BOT_DB_PATH", str(ROOT / "data" / "bot.sqlite3")))
    from .subscriptions import SubscriptionBot
    bot = SubscriptionBot(store, Telegram(token, int(owner), store=store), Freelancer())
    def stop(*_):
        bot.stop.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        bot.run()
    except (APIError, RuntimeError) as e:
        raise SystemExit(str(e)) from None


if __name__ == "__main__":
    main()
