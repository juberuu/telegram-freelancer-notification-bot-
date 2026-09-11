import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from projectbot.api import APIError, Telegram
from projectbot.core import Store, normalize
from projectbot.statistics import WINDOWS, category_for, project_counts, render_counts
from projectbot.subscriptions import SubscriptionBot

NOW = 2_000_000


def project(pid, created=NOW, skill="Python", maximum=500):
    return normalize({"id": pid, "title": "New project", "jobs": [{"name": skill}],
                      "description": "Work needed", "status": "active", "type": "fixed",
                      "time_submitted": created, "budget": {"minimum": 100, "maximum": maximum},
                      "currency": {"code": "USD"}}, {})


class StatisticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "bot.db"
        self.store = Store(self.path)
        self.cfg = {**self.store.get("config"), "keywords": []}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_rolling_boundaries_and_repeated_scans_count_unique_projects(self):
        ages = [0, 1800, 1801, 3600, 3601, 86400, 86401, 259200, 259201, 604800, 604801]
        projects = [project(i, NOW - age) for i, age in enumerate(ages)]
        self.store.record_history(projects, now=NOW)
        self.store.record_history(projects, now=NOW)
        report = project_counts(self.store, self.cfg, now=NOW)
        self.assertEqual(report["totals"], [2, 4, 6, 8, 10])
        for i in range(len(WINDOWS)):
            self.assertEqual(sum(v[i] for v in report["counts"].values()), report["totals"][i])

    def test_history_survives_restart_and_reposted_id_does_not_become_new(self):
        self.store.record_history([project(1, NOW - 2 * 86400)], now=NOW)
        self.store.db.close()
        self.store = Store(self.path)
        self.store.record_history([project(1, NOW)], now=NOW)
        self.assertEqual(project_counts(self.store, self.cfg, now=NOW)["totals"], [0, 0, 0, 1, 1])

    def test_personal_filters_apply_but_five_minute_alert_age_does_not(self):
        self.store.record_history([project(1, NOW - 7200), project(2, skill="Graphic Design"),
                                   project(3, maximum=50)], now=NOW)
        cfg = {**self.cfg, "keywords": ["Python"]}
        report = project_counts(self.store, cfg, now=NOW)
        self.assertEqual(report["totals"], [0, 0, 1, 1, 1])
        self.assertEqual(report["counts"]["design"], [0] * 5)
        self.assertEqual(project_counts(self.store, {**self.cfg, "keywords": ["Graphic Design"]}, now=NOW)["totals"], [1] * 5)

    def test_primary_categories_and_unknown_fallback(self):
        for skill, expected in (("Graphic Design", "design"), ("Embedded Systems", "embedded"),
                                ("Social Media Marketing", "marketing"), ("Python", "software"),
                                ("Machine Learning", "ai_data"), ("Unrecognized", "other")):
            self.assertEqual(category_for(project(1, skill=skill)), expected)
        p = project(1, skill="Unrecognized")
        p["title"] = "STM32 firmware engineer"
        self.assertEqual(category_for(p), "embedded")
        p["skills"] = ["Graphic Design", "Logo Design", "Marketing"]
        self.assertEqual(category_for(p), "design")

    def test_future_and_missing_dates_are_not_counted(self):
        missing = project(1)
        missing["created"] = None
        self.store.record_history([missing, project(2, NOW + 10), project(3, NOW + 100)], now=NOW)
        self.assertEqual(project_counts(self.store, self.cfg, now=NOW)["totals"], [0] * 5)

    def test_partial_history_is_visible_and_rich_table_has_all_windows(self):
        report = project_counts(self.store, self.cfg, now=NOW)
        text, rich = render_counts(report)
        self.assertIn("History starts", text)
        self.store.record_history([project(1)], now=NOW)
        text, rich = render_counts(project_counts(self.store, self.cfg, now=NOW + 1800))
        self.assertIn("<th>30m</th>", rich["html"])
        for label in ("1h", "1d", "3d", "1w"):
            self.assertIn(f"<th>{label}*</th>", rich["html"])
        self.assertIn("Partial history", text)
        self.assertIn("Updated 24 Jan 13:03", text)
        self.assertIn("tracking since 24 Jan 12:33", text)
        self.assertNotIn("UTC", text)
        self.assertNotIn("UTC", rich["html"])
        self.assertIn("<table striped compact>", rich["html"])
        self.assertLess(len(text), 4096)


class SummaryDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "bot.db")
        self.tg = Telegram("test", 123, self.store)
        self.pacing = patch.object(self.tg, "paced_call", return_value={"message_id": 7})
        self.send = self.pacing.start()
        self.clock = patch("projectbot.app.time.time", return_value=NOW)
        self.time = self.clock.start()
        self.hub = SubscriptionBot(self.store, self.tg, Mock())
        self.command(10, "/start")
        self.command(10, "/skills Python")
        self.store.record_history([project(1), project(2, skill="Graphic Design")], now=NOW)
        self.send.reset_mock()

    def tearDown(self):
        self.clock.stop()
        self.pacing.stop()
        for bot in self.hub.snapshot():
            bot.store.db.close()
        self.tmp.cleanup()

    def command(self, uid, text):
        self.hub.handle({"message": {"chat": {"id": uid, "type": "private"}, "from": {"id": uid}, "text": text}})

    def test_manual_summary_is_personal_and_refresh_edits_same_chat(self):
        self.command(10, "/stats")
        method, payload = self.send.call_args.args
        self.assertEqual(method, "sendRichMessage")
        self.assertEqual(payload["chat_id"], 10)
        expected = render_counts(project_counts(self.store, self.hub.member(10).store.get("config"), now=NOW))[1]
        self.assertEqual(payload["rich_message"], expected)
        self.time.return_value = NOW + 60
        with patch.object(self.tg, "call", return_value=True):
            self.hub.handle({"callback_query": {"id": "tap", "from": {"id": 10}, "data": "stats:refresh",
                             "message": {"chat": {"id": 10, "type": "private"}, "message_id": 7}}})
        self.assertEqual(self.send.call_args.args[0], "editMessageText")
        self.assertEqual(self.send.call_args.args[1]["chat_id"], 10)
        self.assertEqual(self.send.call_args.args[1]["message_id"], 7)
        self.send.reset_mock()
        self.assertFalse(self.hub.member(10).send_stats(message_id=7))
        self.send.assert_not_called()

    def test_automatic_summary_due_every_30_minutes_and_not_on_each_loop(self):
        self.hub.stats_round()
        self.send.assert_not_called()
        self.time.return_value = NOW + 1800
        self.hub.stats_round()
        self.send.assert_called_once()
        self.hub.stats_round()
        self.send.assert_called_once()
        self.assertEqual(self.hub.member(10).store.get("stats_next_at"), NOW + 3600)

    def test_off_and_pause_suppress_auto_but_manual_stats_remain_available(self):
        self.command(10, "/stats off")
        self.time.return_value = NOW + 1800
        self.send.reset_mock()
        self.hub.stats_round()
        self.send.assert_not_called()
        self.command(10, "/stats")
        self.send.assert_called_once()
        self.command(10, "/stats on")
        self.command(10, "/pause")
        self.time.return_value = NOW + 3600
        self.send.reset_mock()
        self.hub.stats_round()
        self.send.assert_not_called()

    def test_failed_summary_respects_retry_delay_without_advancing_schedule(self):
        self.time.return_value = NOW + 1800
        self.send.side_effect = APIError("Telegram", 429, 120)
        with self.assertLogs(level="WARNING"):
            self.hub.stats_round()
        self.assertEqual(self.hub.member(10).store.get("stats_next_at"), NOW + 1800)
        self.hub.stats_round()
        self.send.assert_called_once()
        self.time.return_value = NOW + 1920
        self.send.side_effect = None
        self.hub.stats_round()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.hub.member(10).store.get("stats_next_at"), NOW + 3720)

    def test_queued_project_alerts_have_priority_over_summary(self):
        bot = self.hub.member(10)
        bot.store.enqueue([project(3)])
        self.time.return_value = NOW + 1800
        self.hub.stats_round()
        self.send.assert_not_called()
        bot.store.mark(3, "sent")
        self.hub.stats_round()
        self.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
