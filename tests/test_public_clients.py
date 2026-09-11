import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from projectbot.app import Bot
from projectbot.core import Store, normalize
from projectbot.public_clients import parse_client, enrich, fetch_client
from projectbot.api import APIError


def page(client, pid=42):
    raw = {"projectId": pid, "client": client,
           "bids": [{"verification": {"identityVerified": True}}]}
    data = {"store": {"projectsSeo": {"0": {"documents": {"project": {"rawDocument": raw}}}}}}
    return '<script id="webapp-state" type="application/json">' + json.dumps(data) + '</script>'


class PublicClientTest(unittest.TestCase):
    def test_refresh_updates_known_states_and_country_without_mutating_snapshot(self):
        p = {"client_id": 7, "verified": False, "verifications": {"payment_verified": False, "email_verified": True},
             "country": "India", "country_code": "IN"}
        refreshed = enrich(p, {"client_id": 7, "verifications": {"payment_verified": True, "email_verified": False},
                              "country": "Japan", "country_code": "JP"}, prefer_new=True)
        self.assertIs(refreshed["verified"], True)
        self.assertIs(refreshed["verifications"]["email_verified"], False)
        self.assertEqual((refreshed["country"], refreshed["country_code"]), ("Japan", "JP"))
        self.assertIs(p["verified"], False)
        self.assertEqual(enrich(p, {"client_id": 8, "country": "Japan"}, prefer_new=True), p)

    def test_refresh_does_not_turn_unknown_into_unverified_or_keep_wrong_flag(self):
        p = {"verified": True, "verifications": {"payment_verified": True}, "country": "India", "country_code": "IN"}
        refreshed = enrich(p, {"verifications": {"payment_verified": None}, "country": "Japan"}, prefer_new=True)
        self.assertIs(refreshed["verified"], True)
        self.assertEqual(refreshed["country_code"], "")

    def test_public_states_are_saved_and_displayed_before_failing_avatar_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.db")
            try:
                tg, source = Mock(chat_id=123), Mock()
                bot = Bot(store, tg, source)
                p = normalize({"id": 42, "title": "Project", "time_submitted": time.time()}, {})
                store.save_post(7, p, False, time.time())
                def owner_lookup(project):
                    tg.edit.assert_called_once()
                    self.assertIn("✅ Email", tg.edit.call_args.args[1])
                    self.assertIn("⚠️ Payment", tg.edit.call_args.args[1])
                    self.assertIs(store.post(7)[0]["verifications"]["email_verified"], True)
                    raise APIError("Freelancer", 503)
                source.client_details.side_effect = owner_lookup
                with patch("projectbot.app.fetch_client", return_value={"verifications": {
                        "email_verified": True, "payment_verified": False}}):
                    with self.assertRaises(APIError):
                        bot.enrich_post(7)
                self.assertFalse(store.post(7)[0]["_client_enrichment_done"])
                tg.send.assert_not_called()
            finally:
                store.db.close()

    def test_restart_recovers_recent_unfinished_posts_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.db"
            store = Store(path)
            p = normalize({"id": 42, "title": "Project", "time_submitted": time.time()}, {})
            store.save_post(7, p, False, time.time())
            store.save_post(8, {**p, "_client_enrichment_done": True}, False, time.time())
            store.save_post(9, p, False, time.time() - 90000)
            store.db.close()
            store = Store(path)
            try:
                tg = Mock(chat_id=123)
                bot = Bot(store, tg, None)
                self.assertEqual(bot.client_queue.get_nowait()[1], 7)
                self.assertTrue(bot.client_queue.empty())
                with patch("projectbot.app.fetch_client", return_value={"verifications": {"email_verified": True}}):
                    bot.enrich_post(7)
                self.assertTrue(Bot(store, tg, None).client_queue.empty())
                tg.send.assert_not_called()
            finally:
                store.db.close()

    def test_live_alert_includes_verification_on_first_send(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.db")
            try:
                tg = Mock(chat_id=123)
                tg.send.return_value = {"message_id": 7}
                bot = Bot(store, tg, None)
                p = normalize({"id": 42, "title": "Python project", "status": "active", "type": "fixed",
                               "time_submitted": time.time(), "budget": {"maximum": 250},
                               "currency": {"code": "USD"}}, {})
                with patch("projectbot.app.fetch_client", return_value={"verifications": {
                        "payment_verified": True, "email_verified": False}}) as lookup:
                    self.assertTrue(bot.send_post(p))
                lookup.assert_called_once()
                tg.send.assert_called_once()
                self.assertIn("✅ Payment", tg.send.call_args.args[0])
                self.assertIn("⚠️ Email", tg.send.call_args.args[0])
            finally:
                store.db.close()

    def test_live_alert_still_sends_and_queues_retry_when_lookup_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.db")
            try:
                tg = Mock(chat_id=123)
                tg.send.return_value = {"message_id": 7}
                bot = Bot(store, tg, None)
                p = normalize({"id": 42, "title": "Python project", "status": "active", "type": "fixed",
                               "time_submitted": time.time(), "budget": {"maximum": 250},
                               "currency": {"code": "USD"}}, {})
                with patch("projectbot.app.fetch_client", side_effect=APIError("Freelancer public page")):
                    with self.assertLogs(level="WARNING"):
                        self.assertTrue(bot.send_post(p))
                tg.send.assert_called_once()
                self.assertEqual(bot.client_queue.get_nowait()[1], 7)
            finally:
                store.db.close()

    def test_lookup_timeout_still_sends_and_background_retry_adds_states(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.db")
            try:
                tg = Mock(chat_id=123)
                tg.send.return_value = {"message_id": 9}
                bot = Bot(store, tg, None)
                p = normalize({"id": 42, "title": "Preview"}, {})
                with patch("projectbot.app.fetch_client", side_effect=[
                        APIError("Freelancer public page"),
                        {"verifications": {"email_verified": True, "deposit_made": False}}]):
                    with self.assertLogs(level="WARNING"):
                        bot.send_post(p, preview=True)
                    tg.send.assert_called_once()
                    _, message_id, _ = bot.client_queue.get_nowait()
                    bot.enrich_post(message_id)
                    self.assertEqual(tg.edit.call_args.args[0], 9)
                    self.assertIn("✅ Email", tg.edit.call_args.args[1])
                    self.assertIn("⚠️ Deposit", tg.edit.call_args.args[1])
            finally:
                store.db.close()

    def test_only_exact_project_structured_booleans_are_used(self):
        html = page({"verification": {"paymentVerified": True, "emailVerified": False,
                                      "depositMade": True, "phoneVerified": "true",
                                      "profileComplete": False},
                     "address": {"country": "Japan", "countryCode": "jp"}})
        result = parse_client(html, 42)
        self.assertEqual(result["verifications"], {
            "payment_verified": True, "deposit_made": True,
            "email_verified": False, "profile_complete": False})
        self.assertEqual(result["country"], "Japan")
        self.assertEqual(parse_client(html, 99), {})

    def test_labels_and_broken_or_missing_state_do_not_imply_verification(self):
        for html in ('Identity verified Payment verified', '<script id="webapp-state" type="application/json">broken</script>',
                     '<h1>Login required</h1>', page({"verification": None})):
            self.assertFalse(parse_client(html, 42).get("verifications"))

    def test_known_api_states_take_precedence(self):
        p = {"verified": False, "verifications": {"payment_verified": False}, "country": "Japan", "country_code": "JP"}
        result = enrich(p, {"verifications": {"payment_verified": True, "email_verified": True}, "country": "Canada"})
        self.assertIs(result["verified"], False)
        self.assertIs(result["verifications"]["email_verified"], True)
        self.assertEqual(result["country"], "Japan")
        self.assertNotIn("email_verified", p["verifications"])

    def test_untrusted_url_is_not_requested(self):
        with patch("projectbot.public_clients.urllib.request.urlopen") as request:
            self.assertEqual(fetch_client({"id": 42, "url": "https://other.example/projects/42"}), {})
            request.assert_not_called()

    def test_first_notification_contains_available_client_states(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.db")
            try:
                tg = Mock(chat_id=123)
                tg.send.return_value = {"message_id": 7}
                bot = Bot(store, tg, None)
                p = normalize({"id": 42, "time_submitted": time.time(), "title": "Project"}, {})
                with patch("projectbot.app.fetch_client", return_value={"verifications": {
                        "payment_verified": True, "profile_complete": False}}) as lookup:
                    bot.send_post(p, preview=True)
                    lookup.assert_called_once_with(p, timeout=6)
                    tg.send.assert_called_once()
                    self.assertIn("<b>Client</b>", tg.send.call_args.args[0])
                    self.assertIn("✅ Payment", tg.send.call_args.args[0])
                    self.assertIn("⚠️ Profile", tg.send.call_args.args[0])
                    _, message_id, _ = bot.client_queue.get_nowait()
                    bot.enrich_post(message_id)
                    self.assertIs(store.post(7)[0]["verified"], True)
                    bot.enrich_post(message_id)
                    lookup.assert_called_once()
                    tg.edit.assert_not_called()
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
