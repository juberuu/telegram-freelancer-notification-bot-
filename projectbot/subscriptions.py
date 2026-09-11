"""Private subscriptions with separate settings/history and shared network workers."""
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .api import APIError
from .app import Bot
from .core import Store, matches


class SubscriptionBot(Bot):
    def __init__(self, store, telegram, source):
        super().__init__(store, telegram, source)
        self.members_lock = threading.RLock()
        self.members = {telegram.chat_id: self}
        self.subscriber_dir = store.path.parent / (store.path.stem + "-subscribers")
        for chat_id in store.get("subscribers", []):
            if type(chat_id) is int and chat_id > 0 and chat_id != telegram.chat_id:
                self.member(chat_id, persist=False)

    def member(self, chat_id, persist=True):
        with self.members_lock:
            if chat_id not in self.members:
                store = Store(self.subscriber_dir / f"{chat_id}.sqlite3")
                if not store.get("subscriber_initialized"):
                    cfg = store.get("config")
                    cfg["keywords"] = []
                    store.set("config", cfg)
                    store.set("subscriber_initialized", True)
                bot = Bot(store, self.tg.for_chat(chat_id), self.source)
                bot.stop = self.stop
                bot.client_cache = self.client_cache
                bot.client_locks = self.client_locks
                bot.client_state = self.client_state
                bot.stats_store = self.store
                self.members[chat_id] = bot
                if persist:
                    self.store.set("subscribers", [cid for cid in self.members if cid != self.tg.chat_id])
            return self.members[chat_id]

    def snapshot(self):
        with self.members_lock:
            return list(self.members.values())

    @staticmethod
    def active(bot):
        return bool(bot.store.get("started")) and not bot.store.get("config")["paused"]

    def handle(self, update):
        callback = update.get("callback_query")
        message = (callback or {}).get("message") if callback else update.get("message")
        message = message or {}
        sender = (callback or message).get("from", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if (chat.get("type") != "private" or type(chat_id) is not int or chat_id <= 0
                or sender.get("id") != chat_id or sender.get("is_bot")):
            return
        words = str(message.get("text", "")).strip().split(maxsplit=1)
        command = words[0].split("@")[0].lstrip("/").lower() if words else ""
        with self.members_lock:
            known = chat_id in self.members
        if not known and (callback or command != "start"):
            if not callback:
                self.tg.for_chat(chat_id).send("Send /start to subscribe, then /skills Python, Design to choose your skills.")
            return
        bot = self.member(chat_id)
        try:
            # Explicit base call avoids dispatching back into this method for the owner.
            Bot.handle(bot, update)
        except APIError as e:
            if e.code != 403:
                raise
            bot.store.set("started", False)
            bot.store.set("telegram_error", str(e))

    def scan(self):
        active = [b for b in self.snapshot() if self.active(b)]
        if not active:
            return
        age = max(b.store.get("config")["max_age_minutes"] for b in active)
        projects, warnings = self.source.fetch(age)
        self.store.record_history(projects)
        # Include new subscribers and re-read filters changed during the request.
        self.distribute(projects, warnings)

    def distribute(self, projects, warnings, scanned_at=None, cached=False):
        scanned_at = time.time() if scanned_at is None else scanned_at
        for bot in self.snapshot():
            if not self.active(bot):
                continue
            cfg = bot.store.get("config")
            matched = [p for p in projects if matches(p, cfg)]
            bot.store.enqueue(matched)
            for key, value in (("recent", projects), ("warnings", warnings), ("last_scan", scanned_at),
                               ("fetched", len(projects)), ("matched", len(matched))):
                bot.store.set(key, value)
            if not cached:
                bot.store.set("source_error", None)
            bot.store.prune()
            counts = bot.store.match_counts(matched)
            logging.info("Subscriber %s: %d matches, %d new/pending, %d already sent",
                         bot.tg.chat_id, len(matched), counts["pending"], counts["sent"])
        self.delivery_wake.set()

    def worker(self):
        next_scan = retry_at = last_scan = 0
        failures = 0
        cached = None
        cached_at = None
        while not self.stop.is_set():
            active = [b for b in self.snapshot() if self.active(b)]
            requested = False
            for bot in self.snapshot():
                if bot.wake.is_set():
                    requested = True
                    bot.wake.clear()
            now = time.monotonic()
            if active:
                interval = min(b.store.get("config")["interval_seconds"] for b in active)
                next_scan = min(next_scan, last_scan + interval)
                if requested:
                    # Apply new filters/subscriptions immediately to still-fresh cached data.
                    if cached:
                        self.distribute(*cached, scanned_at=cached_at, cached=True)
                    next_scan = min(next_scan, last_scan + 10)
                if now >= max(next_scan, retry_at):
                    try:
                        scan_started = time.monotonic()
                        age = max(b.store.get("config")["max_age_minutes"] for b in active)
                        cached = self.source.fetch(age)
                        cached_at = time.time()
                        self.store.record_history(cached[0])
                        self.distribute(*cached, scanned_at=cached_at)
                        last_scan = scan_started
                        next_scan = last_scan + interval
                        failures = 0
                    except APIError as e:
                        failures += 1
                        retry_at = time.monotonic() + max(e.retry_after, min(60, 5 * 2 ** min(failures - 1, 4)))
                        for bot in active:
                            bot.store.set("source_error", str(e))
                        logging.warning("%s; shared project scan will retry", e)
            self.stop.wait(1)

    def deliver_round(self):
        for bot in self.snapshot():
            self.deliver_member(bot)

    def deliver_member(self, bot):
        if not self.active(bot) or self.stop.is_set() or time.monotonic() < getattr(bot, "delivery_retry_at", 0):
            return
        try:
            Bot.deliver(bot, max_posts=1)
        except APIError as e:
            bot.store.set("telegram_error", str(e))
            bot.delivery_retry_at = time.monotonic() + e.retry_after
            if e.code == 403:
                bot.store.set("started", False)
            logging.warning("%s; subscriber %s delivery postponed", e, bot.tg.chat_id)

    def delivery_worker(self):
        # Bound concurrency; never run two alert deliveries for the same subscriber.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="alert-send") as executor:
            running = {}
            cursor = 0
            while not self.stop.is_set():
                self.delivery_wake.clear()
                for chat_id, future in list(running.items()):
                    if future.done():
                        future.result()
                        del running[chat_id]
                members = self.snapshot()
                for offset in range(len(members)):
                    index = (cursor + offset) % len(members)
                    bot = members[index]
                    if len(running) >= 4:
                        break
                    if (bot.tg.chat_id not in running and self.active(bot)
                            and time.monotonic() >= getattr(bot, "delivery_retry_at", 0)
                            and bot.store.pending(limit=1)):
                        running[bot.tg.chat_id] = executor.submit(self.deliver_member, bot)
                cursor = (cursor + 1) % len(members)
                if not running:
                    self.stats_round()
                self.delivery_wake.wait(0.2)

    def stats_round(self):
        # At most one summary per round, allowing fresh project alerts to proceed.
        for bot in self.snapshot():
            if not self.active(bot) or self.stop.is_set() or bot.store.pending(limit=1):
                continue
            try:
                if bot.send_stats(automatic=True):
                    return
            except APIError as e:
                bot.store.set("stats_retry_at", time.time() + e.retry_after)
                if e.code == 403:
                    bot.store.set("started", False)
                logging.warning("%s; project-count summary will retry", e)
                return

    def enrich_member_post(self, bot, job, avatar=False):
        priority, message_id, attempts = job
        work_queue = bot.avatar_queue if avatar else bot.client_queue
        try:
            if self.active(bot) and not self.stop.is_set():
                bot.enrich_post(message_id, **({"avatar_only": True} if avatar else {"public_only": True}))
                if not avatar:
                    try:
                        bot.avatar_queue.put_nowait((priority, message_id, 0))
                    except queue.Full:
                        logging.warning("Avatar queue full; verification update was delivered")
        except APIError as e:
            logging.warning("%s; original project alert remains available", e)
            if attempts < 2 and (e.code is None or e.code == 429 or e.code >= 500):
                retry_key = "avatar_retry_at" if avatar else "client_retry_at"
                setattr(bot, retry_key, max(getattr(bot, retry_key, 0), time.monotonic() + e.retry_after))
                try:
                    work_queue.put_nowait((priority, message_id, attempts + 1))
                except queue.Full:
                    pass
            elif not avatar:
                # The owner API may still supply states when the public page fails.
                try:
                    bot.avatar_queue.put_nowait((priority, message_id, 0))
                except queue.Full:
                    pass
        finally:
            work_queue.task_done()

    def client_worker(self):
        # Different saved messages can be enriched independently, including in one chat.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="client-details") as executor, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="client-avatar") as avatar_executor:
            running = {}
            avatar_running = None
            cursor = 0
            while not self.stop.is_set():
                for key, future in list(running.items()):
                    if future.done():
                        future.result()
                        del running[key]
                if avatar_running and avatar_running.done():
                    avatar_running.result()
                    avatar_running = None
                members = self.snapshot()
                for offset in range(len(members)):
                    bot = members[(cursor + offset) % len(members)]
                    if len(running) >= 4:
                        break
                    if not self.active(bot) or time.monotonic() < getattr(bot, "client_retry_at", 0):
                        continue
                    try:
                        job = bot.client_queue.get_nowait()
                    except queue.Empty:
                        continue
                    key = (bot.tg.chat_id, job[1])
                    if key in running:
                        bot.client_queue.put_nowait(job)
                        bot.client_queue.task_done()
                        continue
                    running[key] = executor.submit(self.enrich_member_post, bot, job)
                if avatar_running is None:
                    for offset in range(len(members)):
                        bot = members[(cursor + offset) % len(members)]
                        if not self.active(bot) or time.monotonic() < getattr(bot, "avatar_retry_at", 0):
                            continue
                        try:
                            job = bot.avatar_queue.get_nowait()
                        except queue.Empty:
                            continue
                        avatar_running = avatar_executor.submit(self.enrich_member_post, bot, job, True)
                        break
                cursor = (cursor + 1) % max(1, len(members))
                self.stop.wait(0.1)
            for future in running.values():
                future.result()
            if avatar_running:
                avatar_running.result()
