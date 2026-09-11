import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from projectbot.api import APIError, Freelancer, Telegram
from projectbot.app import Bot, change_filter
from projectbot.core import ROOT, Store, alert, rich_alert, contains, matches, normalize


def sample(pid=42):
    return normalize({"id": pid, "title": "Python API", "status": "active", "type": "fixed", "time_submitted": time.time(), "budget": {"minimum": 50, "maximum": 250}, "currency": {"code": "USD"}, "jobs": [], "description": "FastAPI service", "bid_stats": {"bid_count": 8}}, {})


class FiltersTest(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((ROOT / "config.example.json").read_text())
        self.p = sample()

    def test_any_keyword_and_exclusion(self):
        self.assertTrue(matches(self.p, self.cfg))
        self.cfg["exclude"] = ["fastapi"]
        self.assertFalse(matches(self.p, self.cfg))

    def test_word_boundaries_and_literal_symbols(self):
        self.assertFalse(contains("paid email detail", "AI"))
        self.assertTrue(contains("Build AI-powered tools", "ai"))
        self.assertTrue(contains("C++ firmware", "C++"))
        self.assertTrue(contains("Next.js integration", "Next.js"))

    def test_budget_uses_upper_bound_and_usd_conversion(self):
        self.p["max_usd"] = 99
        self.assertFalse(matches(self.p, self.cfg))
        raw = {"id": 2, "budget": {"maximum": 10000}, "currency": {"code": "INR", "exchange_rate": .012}}
        self.assertEqual(normalize(raw, {})["max_usd"], 120)

    def test_hourly_separate_floor(self):
        self.p.update(type="hourly", max_usd=20)
        self.assertTrue(matches(self.p, self.cfg))
        self.p["max_usd"] = 10
        self.assertFalse(matches(self.p, self.cfg))

    def test_missing_rate_or_client_never_passes_enabled_filter(self):
        self.p["max_usd"] = None
        self.assertFalse(matches(self.p, self.cfg))
        self.cfg["min_fixed_usd"] = 0
        self.assertTrue(matches(self.p, self.cfg))
        self.cfg["verified_only"] = True
        self.assertFalse(matches(self.p, self.cfg))
        self.p["verified"] = True
        self.cfg["countries"] = ["US"]
        self.assertFalse(matches(self.p, self.cfg))
        self.p["country_code"] = "US"
        self.assertTrue(matches(self.p, self.cfg))

    def test_old_future_closed_and_bid_limit(self):
        for changes in ({"created": time.time()-7200}, {"created": time.time()+600}, {"active": False}, {"bids": 30}, {"bids": None}):
            p = {**self.p, **changes}
            cfg = {**self.cfg, "max_bids": 20}
            self.assertFalse(matches(p, cfg))

    def test_invalid_input_and_commands(self):
        for command, value in (("budget", "nan"), ("hourly", "inf"), ("interval", "0"), ("age", "5.5"), ("maxbids", "-1"), ("verified", "maybe"), ("countries", "USA")):
            with self.assertRaises(ValueError):
                change_filter(self.cfg, command, value)
        self.assertEqual(change_filter(self.cfg, "keywords", "python, react")["keywords"], ["python", "react"])
        self.assertEqual(change_filter(self.cfg, "countries", "us, gb")["countries"], ["US", "GB"])
        self.assertEqual(change_filter(self.cfg, "type", "hourly")["project_type"], "hourly")
        self.assertIsNone(change_filter(self.cfg, "maxbids", "off")["max_bids"])

    def test_html_escaped(self):
        self.p["title"] = "<script>& injected"
        text, markup = alert(self.p)
        self.assertIn("&lt;script&gt;&amp;", text)
        self.assertIn('href="https://www.freelancer.com/projects/42"', text)
        self.assertEqual(markup["inline_keyboard"], [])

    def test_client_verifications_show_known_states_only(self):
        p = normalize({"id": 1, "owner_id": 7}, {"7": {"status": {
            "payment_verified": True, "identity_verified": False,
            "deposit_made": True, "email_verified": True,
            "profile_complete": True, "phone_verified": False}}})
        text, _ = alert(p)
        self.assertIn("<b>Client</b>", text)
        row = "⚠️ ID · ✅ Payment · ✅ Deposit · ✅ Email · ✅ Profile · ⚠️ Phone"
        self.assertIn(row, text)
        self.assertIn(row, rich_alert(p)["html"])
        self.assertIs(p["verified"], True)
        p = normalize({"id": 1, "owner_id": 7}, {"7": {"status": {
            "payment_verified": "false", "phone_verified": 0}}})
        self.assertNotIn("<b>Client</b>", alert(p)[0])
        self.assertIsNone(p["verified"])

    def test_legacy_payment_verification_still_displays(self):
        p = sample()
        p.pop("verifications", None)
        p["verified"] = False
        text, _ = alert(p)
        self.assertIn("⚠️ Payment", text)

    def test_profile_completion_is_distinct_from_verification(self):
        for value, expected in ((True, "✅ Profile"), (False, "⚠️ Profile"), (None, None)):
            p = normalize({"id": 1, "owner_id": 7}, {"7": {"status": {
                "payment_verified": True, "identity_verified": True,
                "email_verified": True, "phone_verified": False, "profile_complete": value}}})
            text, _ = alert(p)
            for label in ("✅ Payment", "✅ ID", "✅ Email", "⚠️ Phone"):
                self.assertIn(label, text)
            if expected:
                self.assertIn(expected, text)
            else:
                self.assertNotIn("Profile", text)
            self.assertNotIn("Profile verified", text)

    def test_alert_long_untrusted_content_stays_bounded(self):
        import html
        self.p.update(title="<b>" * 200, description="<script>& 😀 " * 1000,
                      skills=["<tag>" * 100] * 10 + [str(n) for n in range(20)])
        text, markup = alert(self.p, preview=True)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<blockquote", text)
        self.assertNotIn("+16 more", text)
        self.assertLess(len(html.unescape(text).encode("utf-16-le")) // 2, 4096)
        self.assertEqual(markup, {"inline_keyboard": []})

    def test_compact_post_links_title_without_brief_or_buttons(self):
        self.p.update(title='Build <app> & "tools"', description="Brief must not appear",
                      url='https://www.freelancer.com/projects/42?a=1&b="two"')
        text, markup = alert(self.p)
        rich = rich_alert(self.p)["html"]
        link = '<a href="https://www.freelancer.com/projects/42?a=1&amp;b=&quot;two&quot;">Build &lt;app&gt; &amp; &quot;tools&quot;</a>'
        self.assertIn(f"<b>{link}</b>", text)
        self.assertIn(f"<h2>{link}</h2>", rich)
        for rendered in (text, rich):
            self.assertNotIn("Brief must not appear", rendered)
            self.assertNotIn("Project brief", rendered)
            self.assertNotIn("View project", rendered)
        self.assertEqual(markup, {"inline_keyboard": []})

    def test_plain_skills_escape_each_item_in_both_layouts(self):
        self.p.update(skills=["Python", "A&B <CAD>", "Python", "Unknown"])
        text, _ = alert(self.p)
        rich = rich_alert(self.p)["html"]
        self.assertIn("Python · A&amp;B &lt;CAD&gt;", text)
        self.assertNotIn("<mark>", text)
        self.assertIn('Python · A&amp;B &lt;CAD&gt;', rich)
        self.assertEqual(rich.split('<b>Skills</b>', 1)[1].count('Python'), 1)
        self.assertNotIn('Unknown', rich)
        self.assertIn("💰 <b>", text)

    def test_alert_missing_metadata_and_hourly_conversion(self):
        self.p.update(created=None, bids=None, minimum=None, maximum=None,
                      description="", skills=[])
        text, _ = alert(self.p)
        for label in ("Posting time unavailable", "Bids unavailable", "Budget unavailable",
                      "Payment status unavailable", "No description provided"):
            self.assertNotIn(label, text)
        self.assertNotIn("<blockquote", text)
        self.assertNotIn("Client country unavailable", text)
        self.assertNotIn("Freelancer API did not provide client details", text)
        self.p.update(type="hourly", minimum=15, maximum=30, currency="EUR", max_usd=35,
                      created=1000, bids=23, country="Italy", country_code="IT")
        text, _ = alert(self.p, now=1000)
        self.assertIn("15–30 EUR / hour", text)
        row = "💰 <b>15–30 EUR / hour</b> · Hourly · 🕒 1970-01-01 09:16:40\n🇮🇹 Italy · 👥 23 bids"
        self.assertIn(row, text)
        rich = rich_alert(self.p, now=1000)["html"]
        self.assertIn("<p>" + row.replace("\n", "<br>") + "</p>", rich)
        self.assertIn("<hr/>", rich)
        self.assertNotIn("────────", rich)
        self.assertNotIn("────────", text)
        for rendered in (text, rich):
            self.assertIn("🇮🇹 Italy", rendered)
            self.assertNotIn("🌍", rendered)
            self.assertNotIn("$35", rendered)
            self.assertNotIn("USD upper", rendered)
            self.assertNotIn("≈", rendered)

    def test_section_headings_and_spaced_skill_rows(self):
        self.p.update(skills=["Python", "React", "Docker", "3D File Scaling and Print Preparation", "SQL"],
                      verifications={"email_verified": True})
        text, _ = alert(self.p)
        rich = rich_alert(self.p)["html"]
        self.assertIn("🛡 <b>Client</b>\n", text)
        self.assertIn("🏷 <b>Skills</b>\n", text)
        self.assertIn("Python · React · Docker · 3D File Scaling", text)
        self.assertIn("Python · React · Docker · 3D File Scaling", rich)
        self.assertNotIn("<br><br>", rich)
        self.assertEqual(rich.count('<blockquote>'), 1)
        self.assertIn('<blockquote>🏷 <b>Skills</b><br>', rich)
        self.assertIn('<p>🛡 <b>Client</b><br>', rich)
        for decoration in ('<mark>', '<code>', '<tg-button'):
            self.assertNotIn(decoration, rich)


class FakeTelegram:
    chat_id = 123
    def __init__(self):
        self.sent = []
        self.fail = False
    def send(self, text, markup=None, **kwargs):
        if self.fail:
            raise APIError("Telegram", 429, 2)
        self.sent.append(text)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.db"
        self.store = Store(self.path)
        self.tg = FakeTelegram()
        self.bot = Bot(self.store, self.tg, None)
        self.public_lookup = patch("projectbot.app.fetch_client", return_value={})
        self.public_lookup.start()
        self.addCleanup(self.public_lookup.stop)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_failed_send_stays_pending_then_survives_restart(self):
        p = sample()
        self.store.enqueue([p, p])
        self.assertEqual(len(self.store.pending()), 1)
        self.tg.fail = True
        with self.assertRaises(APIError):
            self.bot.deliver()
        self.assertEqual(len(self.store.pending()), 1)
        self.tg.fail = False
        with patch.object(self.bot.stop, "wait", return_value=False):
            self.bot.deliver()
        self.store.db.close()
        self.store = Store(self.path)
        self.store.enqueue([p])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.counts()["sent"], 1)

    def test_pause_and_changed_filters_prevent_queued_send(self):
        self.store.enqueue([sample()])
        cfg = self.store.get("config")
        cfg["paused"] = True
        self.store.set("config", cfg)
        self.bot.deliver()
        self.assertEqual(len(self.store.pending()), 1)
        cfg.update(paused=False, min_fixed_usd=1000)
        self.store.set("config", cfg)
        self.bot.deliver()
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.store.counts()["skipped"], 1)

    def test_each_new_match_is_sent_once_across_cycles(self):
        from unittest.mock import Mock
        projects = [sample(pid) for pid in range(25)]
        self.bot.source = Mock()
        self.bot.source.fetch.return_value = (projects, [])
        with patch.object(self.bot.stop, "wait", return_value=False):
            self.bot.scan()
            self.bot.deliver()
            self.assertEqual(len(self.tg.sent), 25)
            self.assertEqual(self.store.pending(), [])
            self.bot.scan()
            self.bot.deliver()
        self.assertEqual(len(self.tg.sent), 25)
        self.assertEqual(self.store.get("matched"), 25)
        self.bot.source.fetch.return_value = (projects + [sample(99)], [])
        with patch.object(self.bot.stop, "wait", return_value=False):
            self.bot.scan()
            counts = self.store.match_counts(projects + [sample(99)])
            self.assertEqual(counts["pending"], 1)
            self.assertEqual(counts["sent"], 25)
            self.bot.deliver()
        self.assertEqual(len(self.tg.sent), 26)

    def test_new_process_does_not_replay_sent_matches(self):
        from unittest.mock import Mock
        projects = [sample(1), sample(2)]
        source = Mock()
        source.fetch.return_value = (projects, [])
        self.bot.source = source
        with patch.object(self.bot.stop, "wait", return_value=False):
            self.bot.scan()
            self.assertEqual(self.store.match_counts(projects)["pending"], 2)
            self.bot.deliver()
            self.bot.scan()
            self.assertEqual(self.store.match_counts(projects)["sent"], 2)
            self.assertEqual(self.store.match_counts(projects)["pending"], 0)
            self.bot.deliver()
        self.assertEqual(len(self.tg.sent), 2)

        restarted = Bot(self.store, self.tg, source)
        with patch.object(restarted.stop, "wait", return_value=False):
            restarted.scan()
            restarted.deliver()
            restarted.scan()
            restarted.deliver()
        self.assertEqual(len(self.tg.sent), 2)

    def test_failed_initial_scan_can_retry(self):
        from unittest.mock import Mock
        source = Mock()
        source.fetch.side_effect = [APIError("Freelancer"), ([sample(1)], [])]
        self.bot.source = source
        with self.assertRaises(APIError):
            self.bot.scan()
        self.bot.scan()
        self.assertEqual(len(self.store.pending()), 1)

    def test_old_projects_are_not_sent_at_startup_or_from_existing_queue(self):
        from unittest.mock import Mock
        old = sample(1)
        old["created"] -= 40 * 60
        fresh = sample(2)
        self.store.enqueue([old])
        self.bot.source = Mock()
        self.bot.source.fetch.return_value = ([old, fresh], [])
        with patch.object(self.bot.stop, "wait", return_value=False):
            self.bot.scan()
            self.bot.deliver()
        self.assertEqual(self.store.get("config")["max_age_minutes"], 5)
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(self.store.counts()["skipped"], 1)

    def test_project_expiring_before_send_is_not_sent(self):
        p = sample()
        p["created"] = 1000
        self.store.enqueue([p])
        with patch("projectbot.app.time.time", side_effect=[1299, 1301]):
            self.bot.deliver()
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.store.counts()["skipped"], 1)

    def test_new_arrival_is_delivered_before_older_queue_entries(self):
        older = sample(1)
        older["created"] -= 60
        self.store.enqueue([sample(2), older])
        order = []
        def send(project, preview=False):
            order.append(project["id"])
            if len(order) == 1:
                self.store.enqueue([sample(3)])
        with patch.object(self.bot, "send_post", side_effect=send), patch.object(self.bot.stop, "wait", return_value=False):
            self.bot.deliver()
        self.assertEqual(order, [2, 3, 1])

    def test_scanning_runs_while_delivery_is_pending(self):
        self.store.set("started", True)
        self.store.enqueue([sample()])
        with patch.object(self.bot, "scan") as scan, patch.object(self.bot.wake, "wait", side_effect=lambda _: self.bot.stop.set()):
            self.bot.worker()
        scan.assert_called_once()

    def test_fast_interval_upgrade_preserves_later_user_changes(self):
        self.assertEqual(self.store.get("config")["interval_seconds"], 10)
        cfg = self.store.get("config")
        cfg["interval_seconds"] = 90
        self.store.set("config", cfg)
        Bot(self.store, self.tg, None)
        self.assertEqual(self.store.get("config")["interval_seconds"], 90)

    def test_partial_cycle_retry_does_not_repeat_successful_posts(self):
        from unittest.mock import Mock
        self.bot.source = Mock()
        self.bot.source.fetch.return_value = ([sample(1), sample(2), sample(3)], [])
        self.bot.scan()
        original = self.tg.send
        calls = 0
        def fail_second(text, markup=None, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise APIError("Telegram", 429, 2)
            return original(text, markup, **kwargs)
        with patch.object(self.bot.stop, "wait", return_value=False):
            with patch.object(self.tg, "send", side_effect=fail_second):
                with self.assertRaises(APIError):
                    self.bot.deliver()
            self.assertEqual(len(self.tg.sent), 1)
            self.store.db.close()
            self.store = Store(self.path)
            self.bot.store = self.store
            self.bot.deliver()
        self.assertEqual(len(self.tg.sent), 3)
        self.assertEqual(self.store.pending(), [])

    def test_full_skills_and_legacy_button_after_restart(self):
        from unittest.mock import Mock
        p = sample()
        p["skills"] = ["Python", "React", "SQL", "Docker", "Linux", "Adobe Photoshop"]
        self.tg.send = Mock(return_value={"message_id": 80})
        self.tg.call = Mock()
        self.bot.send_post(p, preview=True)
        collapsed, keyboard = self.tg.send.call_args.args
        self.assertIn("Adobe Photoshop", collapsed)
        self.assertEqual(keyboard["inline_keyboard"], [])
        self.assertIn('href="', collapsed)
        self.store.db.close()
        self.store = Store(self.path)
        self.bot.store = self.store
        query = {"id": "tap", "from": {"id": 123}, "data": "skills:more",
                 "message": {"message_id": 80, "chat": {"id": 123, "type": "private"}}}
        self.bot.handle({"callback_query": query})
        method, payload = self.tg.call.call_args.args
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["message_id"], 80)
        self.assertIn("Adobe Photoshop", payload["text"])
        self.assertIn("Preview", payload["text"])
        self.assertEqual(payload["reply_markup"]["inline_keyboard"], [])
        query["data"] = "skills:less"
        self.bot.handle({"callback_query": query})
        self.assertEqual(self.tg.call.call_args.args[1]["text"], collapsed)
        self.tg.call.reset_mock()
        query["from"] = {"id": 999}
        self.bot.handle({"callback_query": query})
        self.tg.call.assert_not_called()

    def test_expired_skill_toggle_acknowledges_without_edit(self):
        from unittest.mock import Mock
        self.tg.call = Mock()
        self.bot.handle({"callback_query": {"id": "tap", "from": {"id": 123},
                         "data": "skills:more", "message": {"message_id": 999,
                         "chat": {"id": 123, "type": "private"}}}})
        self.tg.call.assert_called_once()
        self.assertEqual(self.tg.call.call_args.args[0], "answerCallbackQuery")

    def test_private_owner_only(self):
        def update(chat, sender, kind):
            return {"message": {"chat": {"id": chat, "type": kind}, "from": {"id": sender}, "text": "/start"}}
        for msg in (update(999, 999, "private"), update(123, 999, "private"), update(123, 123, "group")):
            self.bot.handle(msg)
        self.assertIsNone(self.store.get("started"))
        self.assertEqual(self.tg.sent, [])
        self.bot.handle(update(123, 123, "private"))
        self.assertTrue(self.store.get("started"))
        self.assertEqual(len(self.tg.sent), 1)


class TelegramRetryTest(unittest.TestCase):
    def test_rate_limit_blocks_other_methods_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "retry.db")
            try:
                tg = Telegram("test", 123, store)
                with patch("projectbot.api.time.time", return_value=1000), patch(
                        "projectbot.api.request", side_effect=APIError("Telegram sendMessage", 429, 7200)) as req:
                    with self.assertRaises(APIError):
                        tg.call("sendMessage")
                    restored = Telegram("test", 123, store)
                    with self.assertRaises(APIError) as error:
                        restored.call("getUpdates")
                    self.assertGreaterEqual(error.exception.retry_after, 7200)
                    self.assertEqual(req.call_count, 1)
                with patch("projectbot.api.time.time", return_value=8202), patch(
                        "projectbot.api.request", return_value={"ok": True, "result": []}) as req:
                    self.assertEqual(restored.call("getUpdates"), [])
                    req.assert_called_once()
            finally:
                store.db.close()

    def test_api_body_rate_limit_also_sets_cooldown(self):
        tg = Telegram("test", 123)
        with patch("projectbot.api.request", return_value={"ok": False, "error_code": 429,
                   "parameters": {"retry_after": 90}}) as req:
            with self.assertRaises(APIError):
                tg.call("getUpdates")
            with self.assertRaises(APIError):
                tg.call("sendMessage")
            self.assertEqual(req.call_count, 1)

    def test_all_sends_share_pacing(self):
        tg = Telegram("test", 123)
        clock = [100.0]
        def advance(delay):
            clock[0] += delay
        with patch("projectbot.api.time.monotonic", side_effect=lambda: clock[0]), patch(
                "projectbot.api.time.sleep", side_effect=advance) as sleep, patch(
                "projectbot.api.request", return_value={"ok": True, "result": {"message_id": 1}}):
            tg.send("Project")
            tg.send("Command reply")
            sleep.assert_called_once_with(1.5)

    def test_different_recipients_do_not_share_the_per_chat_delay(self):
        tg = Telegram("test", 123)
        clock = [100.0]
        def advance(delay):
            clock[0] += delay
        with patch("projectbot.api.time.monotonic", side_effect=lambda: clock[0]), patch(
                "projectbot.api.time.sleep", side_effect=advance) as sleep, patch(
                "projectbot.api.request", return_value={"ok": True, "result": {"message_id": 1}}):
            tg.for_chat(10).send("First")
            tg.for_chat(20).send("Second")
        self.assertAlmostEqual(sleep.call_args.args[0], 0.1)

    def test_slow_network_request_does_not_hold_other_chat_lock(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        tg = Telegram("test", 123)
        started, release, delivered = threading.Event(), threading.Event(), threading.Event()
        def call(method, payload):
            if payload["chat_id"] == 10:
                started.set()
                release.wait(3)
            else:
                delivered.set()
            return {"message_id": 1}
        with patch.object(tg, "call", side_effect=call), ThreadPoolExecutor(max_workers=2) as pool:
            try:
                first = pool.submit(tg.for_chat(10).send, "Slow")
                self.assertTrue(started.wait(1))
                second = pool.submit(tg.for_chat(20).send, "Fast")
                self.assertTrue(delivered.wait(1))
            finally:
                release.set()
            first.result(timeout=2)
            second.result(timeout=2)

    def test_transient_retry_is_short_but_server_cooldowns_are_preserved(self):
        self.assertEqual(APIError("Telegram").retry_after, 5)
        self.assertEqual(APIError("Freelancer", 503).retry_after, 5)
        self.assertEqual(APIError("Telegram", 429, 7200).retry_after, 7200)

    def test_malformed_api_response_raises_retryable_error_without_crashing_worker(self):
        tg = Telegram("test", 123)
        for response in ({"ok": True}, {"ok": False, "parameters": "bad", "error_code": "bad"}, []):
            with patch("projectbot.api.request", return_value=response), self.assertRaises(APIError):
                tg.call("getMe")
        self.assertEqual(APIError("Telegram", "429", "invalid").retry_after, 30)

class SourceTest(unittest.TestCase):
    def test_client_metadata_is_used_when_api_supplies_it(self):
        row = {"id": 1, "owner_id": 7, "status": "active"}
        user = {"location": {"country": {"name": "Japan", "code": "JP"}},
                "status": {"payment_verified": True}}
        response = {"status": "success", "result": {"projects": [row], "users": {"7": user}}}
        with patch("projectbot.api.request", return_value=response):
            projects, warnings = Freelancer().fetch()
        self.assertEqual(projects[0]["country"], "Japan")
        self.assertIs(projects[0]["verified"], True)
        self.assertTrue(any("Payment 1/1" in warning for warning in warnings))

    def test_partial_missing_metadata_produces_diagnostic(self):
        response = {"status": "success", "result": {"projects": [{"id": 1}], "users": {}}}
        with patch("projectbot.api.request", return_value=response):
            source = Freelancer()
            source.token = ""
            _, warnings = source.fetch()
        self.assertIn("1/1", " ".join(warnings))
        self.assertIn("FREELANCER_OAUTH_TOKEN is not configured", " ".join(warnings))

    def test_pagination_deduplicates_and_preserves_time_window(self):
        rows = [{"id": n, "status": "active", "time_submitted": time.time()} for n in range(100)]
        responses = [{"status": "success", "result": {"projects": rows, "total_count": 101}}, {"status": "success", "result": {"projects": [rows[-1]], "total_count": 101}}]
        with patch("projectbot.api.request", side_effect=responses) as req:
            projects, _ = Freelancer().fetch()
        self.assertEqual(len(projects), 100)
        self.assertEqual(req.call_count, 2)
        from urllib.parse import parse_qs, urlparse
        first = parse_qs(urlparse(req.call_args_list[0].args[0]).query)
        second = parse_qs(urlparse(req.call_args_list[1].args[0]).query)
        self.assertEqual(first["to_time"], second["to_time"])
        self.assertEqual(second["offset"], ["100"])

    def test_failure_is_not_empty_success(self):
        with patch("projectbot.api.request", return_value={"status": "error"}):
            with self.assertRaises(APIError):
                Freelancer().fetch()


if __name__ == "__main__":
    unittest.main()
