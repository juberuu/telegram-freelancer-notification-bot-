import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from projectbot.api import Telegram, Freelancer, APIError
from projectbot.app import Bot
from projectbot.core import Store, normalize, avatar_url, alert, rich_alert
from projectbot.public_clients import parse_client, enrich

PHOTO = "https://cdn2.f-cdn.com/ppic/123/logo/client.jpg"
BIDDER_PHOTO = "https://cdn2.f-cdn.com/ppic/456/logo/bidder.jpg"


class AvatarTest(unittest.TestCase):
    def test_api_uses_only_owner_photo_and_normalizes_url(self):
        p = normalize({"id": 42, "owner_id": 7}, {
            "7": {"avatar_large_cdn": "//cdn2.f-cdn.com/ppic/123/logo/client.jpg"},
            "8": {"avatar_large_cdn": BIDDER_PHOTO}})
        self.assertEqual(p["client_avatar_url"], PHOTO)
        self.assertEqual(rich_alert(p)["media"][0]["media"]["media"], PHOTO)
        self.assertEqual(p["client_id"], 7)
        self.assertIsNone(normalize({"id": 42}, {"8": {"avatar": BIDDER_PHOTO}})["client_avatar_url"])

    def test_placeholder_and_untrusted_urls_are_ignored(self):
        for url in (None, "", "/img/unknown.png", "https://example.com/a.jpg",
                    "https://f-cdn.com.evil.example/a.jpg", "file:///a.jpg",
                    "https://user:pass@cdn2.f-cdn.com/a.jpg", "https://[broken",
                    "https://www.f-cdn.com/assets/flags/in.png"):
            self.assertIsNone(avatar_url(url))
        self.assertEqual(avatar_url(PHOTO.replace("https:", "http:")), PHOTO)

    def test_public_parser_never_uses_bidder_photo(self):
        raw = {"projectId": 42, "client": {"verification": {"emailVerified": True}},
               "bids": [{"profileLogoUrl": BIDDER_PHOTO}]}
        def page():
            return '<script id="webapp-state" type="application/json">' + json.dumps({"rawDocument": raw}) + '</script>'
        self.assertNotIn("client_avatar_url", parse_client(page(), 42))
        raw["client"]["profileLogoUrl"] = PHOTO
        self.assertEqual(parse_client(page(), 42)["client_avatar_url"], PHOTO)
        self.assertEqual(parse_client(page(), 99), {})

    def test_preview_is_above_full_text_and_preserved_on_edit(self):
        tg = Telegram("test", 123)
        text = "All skills and description " * 65
        markup = {"inline_keyboard": [[{"text": "View project", "url": "https://www.freelancer.com/projects/42"}]]}
        with patch.object(tg, "paced_call") as call:
            tg.send(text, markup, avatar_url=PHOTO)
            sent = call.call_args.args[1]
            self.assertEqual(sent["text"], text)
            self.assertEqual(sent["reply_markup"], markup)
            self.assertEqual(sent["link_preview_options"]["url"], PHOTO)
            self.assertTrue(sent["link_preview_options"]["show_above_text"])
            tg.edit(8, text, markup, avatar_url=PHOTO)
            self.assertEqual(call.call_args.args[1]["link_preview_options"], sent["link_preview_options"])
            tg.send(text, markup)
            self.assertEqual(call.call_args.args[1]["link_preview_options"], {"is_disabled": True})

    def test_late_avatar_updates_original_message_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bot.db"
            store = Store(path)
            try:
                tg = Mock(chat_id=123)
                tg.send.return_value = {"message_id": 7}
                bot = Bot(store, tg, None)
                project = normalize({"id": 42, "title": "Project"}, {})
                with patch("projectbot.app.fetch_client", return_value={}):
                    bot.send_post(project, preview=True)
                bot.client_cache.clear()
                with patch("projectbot.app.fetch_client", return_value={"client_avatar_url": PHOTO}):
                    bot.enrich_post(7)
                tg.send.assert_called_once()
                self.assertEqual(tg.edit.call_args.args[0], 7)
                self.assertEqual(tg.edit.call_args.kwargs["avatar_url"], PHOTO)
                store.db.close()
                store = Store(path)
                self.assertEqual(store.post(7)[0]["client_avatar_url"], PHOTO)
                self.assertEqual(enrich(store.post(7)[0], {})["client_avatar_url"], PHOTO)
            finally:
                store.db.close()

    def test_source_requests_owner_avatars(self):
        with patch("projectbot.api.request", return_value={"status": "success", "result": {"projects": []}}) as req:
            Freelancer().fetch()
        self.assertIn("user_avatar=true", req.call_args.args[0])

    def test_rich_send_attaches_photo_and_keeps_full_skills(self):
        p = normalize({"id": 42, "title": "<Design> & build", "jobs": [
            {"name": f"Skill {i}"} for i in range(80)]}, {})
        p["client_avatar_url"] = PHOTO
        rich = rich_alert(p)
        self.assertIn('<h2><a href="https://www.freelancer.com/projects/42">&lt;Design&gt; &amp; build</a></h2>', rich["html"])
        self.assertIn("Skill 79", rich["html"])
        self.assertIn('tg://photo?id=client_avatar', rich["html"])
        self.assertNotIn("Unknown", rich["html"])
        self.assertNotIn("show more", rich["html"].lower())
        tg = Telegram("test", 123)
        with patch.object(tg, "paced_call", return_value={"message_id": 9}) as call:
            result = tg.send(*alert(p), rich_message=rich)
            self.assertEqual(result["_render_mode"], "rich")
            self.assertEqual(call.call_args.args[0], "sendRichMessage")
            self.assertEqual(call.call_args.args[1]["rich_message"]["media"][0]["media"]["media"], PHOTO)
            self.assertNotIn("link_preview_options", call.call_args.args[1])
            tg.edit(9, *alert(p), rich_message=rich)
            self.assertEqual(call.call_args.args[1]["rich_message"], rich)

    def test_unknown_photo_does_not_create_fake_media(self):
        rich = rich_alert(normalize({"id": 42}, {}))
        self.assertNotIn("media", rich)
        self.assertNotIn("<figure", rich["html"])

    def test_rejected_rich_message_falls_back_without_losing_text(self):
        tg = Telegram("test", 123)
        with patch.object(tg, "paced_call", side_effect=[APIError("Telegram", 404), {"message_id": 9}, {}]) as call:
            with self.assertLogs(level="WARNING"):
                tg.send("Full text", rich_message={"html": "<p>Full text</p>"})
            self.assertEqual(call.call_args.args[0], "sendMessage")
            self.assertEqual(call.call_args.args[1]["text"], "Full text")
            tg.send("Next", rich_message={"html": "<p>Next</p>"})
            self.assertEqual(call.call_args.args[0], "sendMessage")

    def test_transient_rich_errors_never_fall_back_and_duplicate(self):
        for code in (None, 429, 500):
            tg = Telegram("test", 123)
            with patch.object(tg, "paced_call", side_effect=APIError("Telegram", code)) as call:
                with self.assertRaises(APIError):
                    tg.send("Project", rich_message={"html": "<p>Project</p>"})
                call.assert_called_once()

    def test_owner_lookup_resolves_each_project_and_caches_results(self):
        source = Freelancer()
        responses = [{"status": "success", "result": {"id": pid, "owner_id": uid}}
                     for pid, uid in ((42, 7), (43, 8))]
        owners = [{"status": "success", "result": {"id": uid, "avatar_large_cdn": photo}}
                  for uid, photo in ((7, PHOTO), (8, BIDDER_PHOTO))]
        with patch("projectbot.api.request", side_effect=[responses[0], owners[0], responses[1], owners[1]]) as req:
            a = source.client_details({"id": 42})
            b = source.client_details({"id": 43})
            self.assertEqual(a["client_avatar_url"], PHOTO)
            self.assertEqual(b["client_avatar_url"], BIDDER_PHOTO)
            self.assertEqual(source.client_details({"id": 42}), a)
            self.assertEqual(req.call_count, 4)
            self.assertIn("/users/7/?avatar=true", req.call_args_list[1].args[0])
            self.assertIn("/users/8/?avatar=true", req.call_args_list[3].args[0])

    def test_known_owner_skips_project_request_and_rejects_wrong_user(self):
        source = Freelancer()
        with patch("projectbot.api.request", return_value={"status": "success", "result": {
                "id": 8, "avatar": BIDDER_PHOTO}}) as req:
            self.assertFalse(source.client_details({"id": 42, "client_id": 7}).get("client_avatar_url"))
            req.assert_called_once()
            self.assertIn("/users/7/", req.call_args.args[0])

    def test_hidden_owner_never_guesses_from_other_users(self):
        source = Freelancer()
        with patch("projectbot.api.request", return_value={"status": "success", "result": {
                "project": {"id": 42, "owner_id": None},
                "users": {"8": {"avatar": BIDDER_PHOTO}}}}) as req:
            self.assertFalse(source.client_details({"id": 42}).get("client_avatar_url"))
            req.assert_called_once()

    def test_client_lookup_respects_shared_cooldown(self):
        source = Freelancer()
        with patch("projectbot.api.request", side_effect=APIError("Freelancer", 429, 120)) as req:
            for pid in (42, 43):
                with self.assertRaises(APIError):
                    source.client_details({"id": pid})
            req.assert_called_once()

    def test_background_owner_lookup_attaches_avatar_to_original_post(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "bot.db")
            try:
                tg = Mock(chat_id=123)
                tg.send.return_value = {"message_id": 7}
                source = Mock()
                source.client_details.return_value = {"client_avatar_url": PHOTO, "client_id": 123}
                bot = Bot(store, tg, source)
                with patch("projectbot.app.fetch_client", return_value={}):
                    bot.send_post(normalize({"id": 42}, {}), preview=True)
                    source.client_details.assert_not_called()
                    bot.enrich_post(7)
                source.client_details.assert_called_once()
                tg.send.assert_called_once()
                self.assertEqual(tg.edit.call_args.args[0], 7)
                rich = tg.edit.call_args.kwargs["rich_message"]
                self.assertEqual(rich["media"][0]["media"]["media"], PHOTO)
                self.assertEqual(store.post(7)[0]["client_id"], 123)
            finally:
                store.db.close()


if __name__ == "__main__":
    unittest.main()
