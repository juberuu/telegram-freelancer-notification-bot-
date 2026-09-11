import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from projectbot.api import APIError, Telegram
from projectbot.core import Store, normalize
from projectbot.subscriptions import SubscriptionBot


def project(pid, skill):
    return normalize({"id": pid, "title": f"Build {skill} project", "description": "Work needed",
                      "jobs": [{"name": skill}], "time_submitted": time.time(), "status": "active",
                      "type": "fixed", "budget": {"minimum": 100, "maximum": 500},
                      "currency": {"code": "USD"}}, {})


class SubscriptionTest(unittest.TestCase):
    def test_price_label_explanation_is_private(self):
        self.command(10, "/start")
        for data, expected in (("price:fixed", "Fixed Price"), ("price:hourly", "Hourly Price")):
            query = {"id": "price-tap", "from": {"id": 10}, "data": data,
                     "message": {"message_id": 7, "chat": {"id": 10, "type": "private"}}}
            with patch.object(self.hub.member(10).tg, "call") as call:
                self.hub.handle({"callback_query": query})
                call.assert_called_once_with("answerCallbackQuery", {
                    "callback_query_id": "price-tap", "text": expected, "show_alert": True})
                call.reset_mock()
                query["from"] = {"id": 20}
                self.hub.handle({"callback_query": query})
                call.assert_not_called()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.sqlite3"
        self.store = Store(self.path)
        self.tg = Telegram("test", 123, self.store)
        self.transport = patch.object(self.tg, "paced_call", return_value={"message_id": 7})
        self.calls = self.transport.start()
        self.source = Mock()
        self.source.client_details.return_value = {}
        self.hub = SubscriptionBot(self.store, self.tg, self.source)
        self.lookup = patch("projectbot.app.fetch_client", return_value={})
        self.lookup.start()

    def tearDown(self):
        self.lookup.stop()
        self.transport.stop()
        for bot in self.hub.snapshot():
            bot.store.db.close()
        self.tmp.cleanup()

    def command(self, chat_id, text):
        self.hub.handle({"message": {"chat": {"id": chat_id, "type": "private"},
                                    "from": {"id": chat_id}, "text": text}})

    def posts(self):
        return [call.args[1] for call in self.calls.call_args_list if call.args[0] == "sendRichMessage"]

    def test_subscribers_set_independent_skills_and_receive_only_their_matches(self):
        self.command(10, "/start")
        self.command(20, "/start")
        self.command(10, "/skills Python")
        self.command(20, "/keywords Graphic Design")
        self.assertEqual(self.hub.member(10).store.get("config")["keywords"], ["Python"])
        self.assertEqual(self.hub.member(20).store.get("config")["keywords"], ["Graphic Design"])
        self.assertNotEqual(self.store.get("config")["keywords"], ["Graphic Design"])
        self.source.fetch.return_value = ([project(1, "Python"), project(2, "Graphic Design")], [])
        self.hub.scan()
        self.source.fetch.assert_called_once_with(5)
        self.hub.deliver_round()
        self.assertEqual([p["chat_id"] for p in self.posts()], [10, 20])
        self.assertIn("Python", self.posts()[0]["rich_message"]["html"])
        self.assertNotIn("Graphic Design", self.posts()[0]["rich_message"]["html"])
        self.assertIn("Graphic Design", self.posts()[1]["rich_message"]["html"])
        self.hub.scan()
        self.hub.deliver_round()
        self.assertEqual(len(self.posts()), 2)

    def test_same_project_delivered_once_to_each_matching_person(self):
        for uid in (10, 20):
            self.command(uid, "/start")
            self.command(uid, "/skills Python")
        self.source.fetch.return_value = ([project(1, "Python")], [])
        self.hub.scan()
        self.hub.deliver_round()
        self.hub.deliver_round()
        self.assertEqual(len(self.posts()), 2)
        for uid in (10, 20):
            self.assertEqual(self.hub.member(uid).store.counts()["sent"], 1)
        self.assertEqual(self.store.counts(), {})

    def test_message_ids_and_late_avatar_updates_are_isolated_by_chat(self):
        for uid, skill in ((10, "Python"), (20, "Design")):
            self.command(uid, "/start")
            self.command(uid, "/skills " + skill)
        self.source.fetch.return_value = ([project(1, "Python"), project(2, "Design")], [])
        self.hub.scan()
        self.hub.deliver_round()
        one, two = self.hub.member(10), self.hub.member(20)
        self.assertEqual(one.store.post(7)[0]["id"], 1)
        self.assertEqual(two.store.post(7)[0]["id"], 2)
        photo = "https://cdn2.f-cdn.com/ppic/123/logo/client.jpg"
        self.source.client_details.return_value = {"client_avatar_url": photo}
        one.enrich_post(7)
        method, payload = self.calls.call_args.args
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["chat_id"], 10)
        self.assertIn(photo, str(payload["rich_message"]["media"]))
        self.assertIsNone(two.store.post(7)[0]["client_avatar_url"])

    def test_pause_stop_and_restart_preserve_own_filters_without_stopping_others(self):
        for uid in (10, 20):
            self.command(uid, "/start")
        self.command(10, "/skills Python")
        self.command(10, "/stop")
        self.source.fetch.return_value = ([project(1, "Python")], [])
        self.hub.scan()
        self.hub.deliver_round()
        self.assertEqual([p["chat_id"] for p in self.posts()], [20])
        self.assertFalse(self.hub.active(self.hub.member(10)))
        self.command(10, "/start")
        self.assertEqual(self.hub.member(10).store.get("config")["keywords"], ["Python"])
        self.hub.scan()
        self.command(10, "/pause")
        self.hub.deliver_round()
        self.assertEqual(len(self.posts()), 1)
        self.command(10, "/resume")
        self.hub.deliver_round()
        self.assertEqual(len(self.posts()), 2)

    def test_filters_changed_after_scan_are_rechecked_before_delivery(self):
        self.command(10, "/start")
        self.source.fetch.return_value = ([project(1, "Python")], [])
        self.hub.scan()
        self.command(10, "/skills Graphic Design")
        self.hub.deliver_round()
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.hub.member(10).store.counts()["skipped"], 1)

    def test_blocked_recipient_does_not_prevent_others_receiving_posts(self):
        for uid in (10, 20):
            self.command(uid, "/start")
        self.source.fetch.return_value = ([project(1, "Python")], [])
        self.hub.scan()
        def send(method, payload):
            if payload["chat_id"] == 10:
                raise APIError("Telegram", 403)
            return {"message_id": 7}
        self.calls.side_effect = send
        with self.assertLogs(level="WARNING"):
            self.hub.deliver_round()
        self.assertFalse(self.hub.active(self.hub.member(10)))
        self.assertEqual(self.hub.member(20).store.counts()["sent"], 1)

    def test_subscribers_settings_and_history_survive_restart_owner_history_unchanged(self):
        for uid in (10, 20):
            self.command(uid, "/start")
            self.command(uid, "/skills Design")
        owner_project = project(99, "Python")
        self.store.enqueue([owner_project])
        self.store.mark(99, "sent")
        self.source.fetch.return_value = ([project(1, "Design")], [])
        self.hub.scan()
        self.hub.deliver_round()
        for bot in self.hub.snapshot():
            bot.store.db.close()
        self.store = Store(self.path)
        self.hub = SubscriptionBot(self.store, self.tg, self.source)
        self.assertEqual(set(self.store.get("subscribers")), {10, 20})
        self.assertEqual(self.store.counts()["sent"], 1)
        for uid in (10, 20):
            bot = self.hub.member(uid)
            self.assertEqual(bot.store.get("config")["keywords"], ["Design"])
            self.assertEqual(bot.store.counts()["sent"], 1)
        self.hub.scan()
        self.hub.deliver_round()
        self.assertEqual(len(self.posts()), 2)

    def test_private_self_start_required_and_callbacks_cannot_cross_chats(self):
        self.command(10, "/skills Python")
        self.assertNotIn(10, self.hub.members)
        for chat_id, sender, kind in ((10, 20, "private"), (-10, 10, "group")):
            self.hub.handle({"message": {"chat": {"id": chat_id, "type": kind},
                                        "from": {"id": sender}, "text": "/start"}})
        self.assertEqual(len(self.hub.members), 1)
        self.command(10, "/start")
        self.calls.reset_mock()
        self.hub.handle({"callback_query": {"from": {"id": 20}, "data": "skills:more",
                         "message": {"chat": {"id": 10, "type": "private"}, "message_id": 7}}})
        self.calls.assert_not_called()

    def test_transport_shares_rate_limit_across_subscribers(self):
        # Exercise the real common transport; no request is made to Telegram.
        tg = Telegram("test", 123, self.store)
        with patch("projectbot.api.request", side_effect=APIError("Telegram", 429, 120)) as req:
            for uid in (10, 20):
                with self.assertRaises(APIError):
                    tg.for_chat(uid).call("sendMessage", {"chat_id": uid, "text": "test"})
            req.assert_called_once()

    def test_budget_filters_are_private_and_delivery_rounds_are_fair(self):
        for uid in (10, 20):
            self.command(uid, "/start")
        self.command(10, "/budget 1000")
        self.assertEqual(self.hub.member(20).store.get("config")["min_fixed_usd"], 100)
        self.source.fetch.return_value = ([project(1, "Python"), project(2, "Design")], [])
        self.hub.scan()
        self.hub.deliver_round()
        self.assertEqual([p["chat_id"] for p in self.posts()], [20])
        self.assertEqual(self.hub.member(20).store.counts()["pending"], 1)
        self.command(10, "/budget 100")
        self.hub.scan()
        self.hub.deliver_round()
        self.assertEqual([p["chat_id"] for p in self.posts()], [20, 10, 20])

    def test_shared_worker_runs_without_owner_and_check_cannot_flood_source(self):
        self.command(10, "/start")
        self.command(20, "/start")
        self.assertFalse(self.hub.active(self.hub))
        self.source.fetch.return_value = ([project(1, "Python")], [])
        clock = [100]
        def step(_):
            clock[0] += 1
            for uid in (10, 20):
                self.hub.member(uid).wake.set()
            if clock[0] >= 140:
                self.hub.stop.set()
        with patch("projectbot.subscriptions.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.hub.stop, "wait", side_effect=step):
            self.hub.worker()
        self.assertEqual(self.source.fetch.call_count, 4)
        for uid in (10, 20):
            self.assertEqual(self.hub.member(uid).store.counts()["pending"], 1)

    def test_slow_subscriber_does_not_delay_another_or_duplicate_inflight_project(self):
        import threading
        for uid in (10, 20):
            self.command(uid, "/start")
        self.source.fetch.return_value = ([project(1, "Python")], [])
        self.hub.scan()
        started, release, delivered = threading.Event(), threading.Event(), threading.Event()
        def slow(*args, **kwargs):
            started.set()
            release.wait(3)
            return True
        def fast(*args, **kwargs):
            delivered.set()
            return True
        with patch.object(self.hub.member(10), "send_post", side_effect=slow) as one, \
                patch.object(self.hub.member(20), "send_post", side_effect=fast) as two:
            worker = threading.Thread(target=self.hub.delivery_worker)
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(delivered.wait(1))
            finally:
                self.hub.stop.set()
                self.hub.delivery_wake.set()
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            one.assert_called_once()
            two.assert_called_once()

    def test_cached_redistribution_does_not_hide_source_failure_or_refresh_scan_time(self):
        self.command(10, "/start")
        bot = self.hub.member(10)
        bot.store.set("source_error", "Freelancer unavailable")
        before = time.time() - 100
        self.hub.distribute([project(1, "Python")], [], scanned_at=before, cached=True)
        self.assertEqual(bot.store.get("last_scan"), before)
        self.assertEqual(bot.store.get("source_error"), "Freelancer unavailable")

    def test_scan_cadence_does_not_add_request_duration_to_interval(self):
        self.command(10, "/start")
        clock, starts = [100.0], []
        def fetch(age):
            starts.append(clock[0])
            clock[0] += 8
            return [], []
        def step(_):
            clock[0] += 1
            if clock[0] >= 170:
                self.hub.stop.set()
        self.source.fetch.side_effect = fetch
        with patch("projectbot.subscriptions.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(self.hub.stop, "wait", side_effect=step):
            self.hub.worker()
        self.assertEqual(starts, [100, 110, 120, 130, 140, 150, 160])

    def test_pending_alerts_do_not_starve_client_verification_updates(self):
        self.command(10, "/start")
        bot = self.hub.member(10)
        self.source.fetch.return_value = ([project(1, "Python")], [])
        self.hub.scan()
        self.assertTrue(bot.store.pending(limit=1))
        bot.client_queue.put_nowait((-time.time(), 7, 0))
        def stop_after_update(_, **kwargs):
            self.hub.stop.set()
            return True
        with patch.object(bot, "enrich_post", side_effect=stop_after_update) as update:
            self.hub.client_worker()
        update.assert_called_once_with(7, public_only=True)

    def test_slow_client_lookup_does_not_block_next_post_in_same_chat(self):
        import threading
        self.command(10, "/start")
        bot = self.hub.member(10)
        started, release, updated = threading.Event(), threading.Event(), threading.Event()
        bot.client_queue.put_nowait((-2, 7, 0))
        bot.client_queue.put_nowait((-1, 8, 0))
        def enrich(message_id, **kwargs):
            if message_id == 7:
                started.set()
                release.wait(3)
            else:
                updated.set()
        with patch.object(bot, "enrich_post", side_effect=enrich):
            worker = threading.Thread(target=self.hub.client_worker)
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(updated.wait(1))
            finally:
                self.hub.stop.set()
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_subscribers_share_one_concurrent_public_lookup(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        for uid in (10, 20):
            self.command(uid, "/start")
        started, release = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            started.set()
            release.wait(3)
            return {"verifications": {"email_verified": True}}
        p = project(77, "Python")
        with patch("projectbot.app.fetch_client", side_effect=fetch) as lookup, ThreadPoolExecutor(2) as pool:
            one = pool.submit(self.hub.member(10).client_details, p)
            try:
                self.assertTrue(started.wait(1))
                two = pool.submit(self.hub.member(20).client_details, p)
            finally:
                release.set()
            self.assertEqual(one.result(), two.result())
            lookup.assert_called_once()

    def test_blocked_avatar_does_not_consume_verification_workers(self):
        import threading
        self.command(10, "/start")
        bot = self.hub.member(10)
        started, release, verified = threading.Event(), threading.Event(), threading.Event()
        bot.avatar_queue.put_nowait((-1, 7, 0))
        def enrich(message_id, public_only=False, avatar_only=False):
            if avatar_only:
                started.set()
                release.wait(3)
            elif message_id == 8 and public_only:
                verified.set()
        with patch.object(bot, "enrich_post", side_effect=enrich):
            worker = threading.Thread(target=self.hub.client_worker)
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                bot.client_queue.put_nowait((-2, 8, 0))
                self.assertTrue(verified.wait(1))
            finally:
                self.hub.stop.set()
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())

    def test_public_failure_still_tries_owner_api_after_retries(self):
        self.command(10, "/start")
        bot = self.hub.member(10)
        job = (-1, 7, 2)
        bot.client_queue.put_nowait(job)
        job = bot.client_queue.get_nowait()
        with patch.object(bot, "enrich_post", side_effect=APIError("Freelancer public page", 503)), self.assertLogs(level="WARNING"):
            self.hub.enrich_member_post(bot, job)
        self.assertEqual(bot.avatar_queue.get_nowait(), (-1, 7, 0))


if __name__ == "__main__":
    unittest.main()
