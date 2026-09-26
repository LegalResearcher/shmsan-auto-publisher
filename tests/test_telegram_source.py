import os
import unittest
from datetime import timezone
from unittest.mock import patch

import requests

from telegram_source import (
    CURSOR_KEY,
    MAX_TELEGRAM_DOWNLOAD_BYTES,
    _post_link,
    _to_news_item,
    download_telegram_photo,
    fetch_telegram_items,
    extract_video_url,
    is_configured,
    merge_photo_replies_with_news_items,
)


class FakeResponse:
    def __init__(self, payload=None, chunks=None, ok=True, status_code=200):
        self.payload = payload
        self.chunks = chunks or []
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield from self.chunks


class TelegramSourceTests(unittest.TestCase):
    def test_extracts_supported_video_url(self):
        self.assertEqual(
            extract_video_url("شاهد الفيديو https://youtu.be/example123."),
            "https://youtu.be/example123",
        )
        self.assertEqual(extract_video_url("رابط صورة https://example.com/photo.jpg"), None)

    def test_video_url_on_normal_post_is_carried_to_item(self):
        item = _to_news_item({"update_id": 101, "channel_post": {
            "message_id": 51, "date": 1_750_000_000, "chat": {"id": -1001234567890},
            "text": "عنوان الخبر\nhttps://www.youtube.com/watch?v=abc123"}},
            "-1001234567890")
        self.assertEqual(item["_telegram_video_url"], "https://www.youtube.com/watch?v=abc123")

    def test_video_reply_is_merged_into_original_news_item(self):
        original = _to_news_item({"update_id": 102, "channel_post": {
            "message_id": 52, "date": 1_750_000_000, "chat": {"id": -1001234567890},
            "text": "عنوان خبر الفيديو\nمتن الخبر."}}, "-1001234567890")
        reply = _to_news_item({"update_id": 103, "channel_post": {
            "message_id": 53, "date": 1_750_000_100, "chat": {"id": -1001234567890},
            "text": "https://youtu.be/video123",
            "reply_to_message": {"message_id": 52, "date": 1_750_000_000,
                "chat": {"id": -1001234567890}, "text": "عنوان خبر الفيديو\nمتن الخبر."}}},
            "-1001234567890")
        news, late = merge_photo_replies_with_news_items([original, reply])
        self.assertEqual(late, [])
        self.assertEqual(news[0]["_telegram_video_url"], "https://youtu.be/video123")
    def test_cursor_key_is_independent_for_shmsan_reader(self):
        self.assertEqual(CURSOR_KEY, "shmsan_janoub_voice_channel")

    def test_maps_private_channel_post_to_news_item(self):
        update = {
            "update_id": 91,
            "channel_post": {
                "message_id": 27,
                "date": 1_750_000_000,
                "chat": {"id": -1001234567890, "title": "مصدر خاص"},
                "text": "عنوان الخبر\n\nالفقرة الأولى.\nالفقرة الثانية.",
            },
        }
        item = _to_news_item(update, "-1001234567890")
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "عنوان الخبر")
        self.assertEqual(item["raw_body"], "عنوان الخبر\n\nالفقرة الأولى.\nالفقرة الثانية.")
        self.assertEqual(item["link"], "https://t.me/c/1234567890/27")
        self.assertEqual(item["category"], "أخبار وتقارير")
        self.assertEqual(item["source_feed"], "telegram://-1001234567890")
        self.assertTrue(item["_telegram_source"])
        self.assertEqual(item["_telegram_update_id"], 91)
        self.assertIsNone(item["_telegram_photo_file_id"])
        self.assertEqual(item["pub_date"].tzinfo, timezone.utc)

    def test_selects_largest_photo_file_id(self):
        update = {
            "update_id": 95,
            "channel_post": {
                "message_id": 30,
                "date": 1_750_000_000,
                "chat": {"id": -1001234567890},
                "caption": "صورة الخبر",
                "photo": [
                    {"file_id": "small", "width": 90, "height": 90, "file_size": 3000},
                    {"file_id": "large", "width": 1280, "height": 720, "file_size": 55000},
                ],
            },
        }
        item = _to_news_item(update, "-1001234567890")
        self.assertEqual(item["_telegram_photo_file_id"], "large")

    def test_photo_reply_uses_original_news_text_and_link(self):
        update = {"update_id": 96, "channel_post": {
            "message_id": 46, "date": 1_750_000_100,
            "chat": {"id": -1001234567890},
            "photo": [{"file_id": "reply-photo", "width": 1200, "height": 900}],
            "reply_to_message": {"message_id": 42, "date": 1_750_000_000,
                "chat": {"id": -1001234567890},
                "text": "عنوان الخبر الأصلي\nتفاصيل الخبر."},
        }}
        item = _to_news_item(update, "-1001234567890")
        self.assertTrue(item["_telegram_photo_reply"])
        self.assertEqual(item["title"], "عنوان الخبر الأصلي")
        self.assertEqual(item["link"], "https://t.me/c/1234567890/42")

    def test_same_batch_reply_photo_is_merged(self):
        original = _to_news_item({"update_id": 97, "channel_post": {
            "message_id": 47, "date": 1_750_000_000, "chat": {"id": -1001234567890},
            "text": "عنوان خبر جديد\nمتن الخبر."}}, "-1001234567890")
        reply = _to_news_item({"update_id": 98, "channel_post": {
            "message_id": 48, "date": 1_750_000_100, "chat": {"id": -1001234567890},
            "photo": [{"file_id": "reply-photo", "width": 1200, "height": 900}],
            "reply_to_message": {"message_id": 47, "date": 1_750_000_000,
                "chat": {"id": -1001234567890}, "text": "عنوان خبر جديد\nمتن الخبر."}}},
            "-1001234567890")
        news, late = merge_photo_replies_with_news_items([original, reply])
        self.assertEqual(late, [])
        self.assertEqual(news[0]["_telegram_photo_file_id"], "reply-photo")

    def test_published_source_reply_is_late(self):
        original = _to_news_item({"update_id": 99, "channel_post": {
            "message_id": 49, "date": 1_750_000_000, "chat": {"id": -1001234567890},
            "text": "عنوان منشور"}}, "-1001234567890")
        reply = _to_news_item({"update_id": 100, "channel_post": {
            "message_id": 50, "date": 1_750_000_100, "chat": {"id": -1001234567890},
            "photo": [{"file_id": "reply-photo", "width": 1200, "height": 900}],
            "reply_to_message": {"message_id": 49, "date": 1_750_000_000,
                "chat": {"id": -1001234567890}, "text": "عنوان منشور"}}},
            "-1001234567890")
        _, late = merge_photo_replies_with_news_items([original, reply], {original["link"]})
        self.assertEqual(late, [reply])

    def test_accepts_caption_as_full_raw_body(self):
        update = {
            "update_id": 92,
            "channel_post": {
                "message_id": 28,
                "date": 1_750_000_000,
                "chat": {"id": -1001234567890, "username": "public_source"},
                "caption": "نص الخبر المصاحب للصورة",
            },
        }
        item = _to_news_item(update, "-1001234567890")
        self.assertEqual(item["raw_body"], "نص الخبر المصاحب للصورة")
        self.assertEqual(item["link"], "https://t.me/public_source/28")

    def test_ignores_other_chats_and_non_channel_updates(self):
        wrong_chat = {
            "update_id": 93,
            "channel_post": {
                "message_id": 29,
                "date": 1_750_000_000,
                "chat": {"id": -1009999999999},
                "text": "نص لا يجب أخذه",
            },
        }
        self.assertIsNone(_to_news_item(wrong_chat, "-1001234567890"))
        self.assertIsNone(_to_news_item({"update_id": 94, "message": {}}, "-1001234567890"))

    def test_private_link_without_numeric_channel_prefix(self):
        self.assertEqual(_post_link({"id": -77}, 10), "https://t.me/c/77/10")

    def test_configuration_is_optional_but_must_be_complete(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_configured())
        with patch.dict(os.environ, {"TELEGRAM_SOURCE_BOT_TOKEN": "token"}, clear=True):
            with self.assertRaises(RuntimeError):
                is_configured()

    def test_fetch_uses_persisted_cursor_and_filters_other_chats(self):
        channel_update = {
            "update_id": 81,
            "channel_post": {
                "message_id": 5,
                "date": 1_750_000_000,
                "chat": {"id": -1001234567890},
                "text": "خبر من المصدر",
            },
        }
        other_update = {
            "update_id": 82,
            "channel_post": {
                "message_id": 6,
                "date": 1_750_000_000,
                "chat": {"id": -1005555555555},
                "text": "ليس من المصدر",
            },
        }
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "dedicated-test-token",
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse([{"update_id": 80}]),
                FakeResponse({"ok": True, "result": {"url": "", "pending_update_count": 0}}),
                FakeResponse({"ok": True, "result": {"id": 12345, "username": "Vhjxbxbhsbot"}}),
                FakeResponse({"ok": True, "result": {"status": "administrator"}}),
                FakeResponse({"ok": True, "result": [channel_update, other_update]}),
            ],
        ) as get:
            items, cursor = fetch_telegram_items()
        self.assertEqual(cursor, 82)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "خبر من المصدر")
        self.assertTrue(get.call_args_list[2].args[0].endswith("/getMe"))
        self.assertEqual(get.call_args_list[3].kwargs["params"], {"chat_id": "-1001234567890", "user_id": 12345})
        self.assertEqual(get.call_args_list[4].kwargs["params"]["offset"], 81)
        self.assertEqual(get.call_args_list[4].kwargs["params"]["allowed_updates"], '["channel_post"]')

    def test_removes_active_webhook_without_dropping_pending_updates(self):
        channel_update = {
            "update_id": 91,
            "channel_post": {
                "message_id": 7,
                "date": 1_750_000_000,
                "chat": {"id": -1001234567890},
                "text": "خبر أثناء إصلاح الربط",
            },
        }
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "dedicated-test-token",
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse([]),
                FakeResponse({"ok": True, "result": {"url": "https://old.example/webhook", "pending_update_count": 3}}),
                FakeResponse({"ok": True, "result": True}),
                FakeResponse({"ok": True, "result": {"url": "", "pending_update_count": 3}}),
                FakeResponse({"ok": True, "result": {"id": 12345, "username": "Vhjxbxbhsbot"}}),
                FakeResponse({"ok": True, "result": {"status": "administrator"}}),
                FakeResponse({"ok": True, "result": [channel_update]}),
            ],
        ) as get:
            items, cursor = fetch_telegram_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(cursor, 91)
        self.assertTrue(get.call_args_list[2].args[0].endswith("/deleteWebhook"))
        self.assertEqual(get.call_args_list[2].kwargs["params"], {"drop_pending_updates": "false"})
        self.assertTrue(get.call_args_list[6].args[0].endswith("/getUpdates"))

    def test_conflict_after_webhook_check_identifies_other_poller_without_token(self):
        token = "do-not-leak-polling-token"
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": token,
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse([]),
                FakeResponse({"ok": True, "result": {"url": "", "pending_update_count": 0}}),
                FakeResponse({"ok": True, "result": {"id": 12345, "username": "Vhjxbxbhsbot"}}),
                FakeResponse({"ok": True, "result": {"status": "administrator"}}),
                FakeResponse(
                    {"ok": False, "error_code": 409, "description": "Conflict: terminated by another getUpdates request"},
                    ok=False,
                    status_code=409,
                ),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "another getUpdates request") as context:
                fetch_telegram_items()
        self.assertNotIn(token, str(context.exception))

    def test_rejects_configured_chat_where_bot_is_not_admin(self):
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "dedicated-test-token",
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse([]),
                FakeResponse({"ok": True, "result": {"url": ""}}),
                FakeResponse({"ok": True, "result": {"id": 12345, "username": "Vhjxbxbhsbot"}}),
                FakeResponse({"ok": True, "result": {"status": "left"}}),
            ],
        ) as get:
            with self.assertRaisesRegex(RuntimeError, "is not an administrator"):
                fetch_telegram_items()
        self.assertEqual(get.call_count, 4)

    def test_reports_wrong_or_unreachable_channel_id_before_polling(self):
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "dedicated-test-token",
            "TELEGRAM_SOURCE_CHAT_ID": "4430613399",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse([]),
                FakeResponse({"ok": True, "result": {"url": ""}}),
                FakeResponse({"ok": True, "result": {"id": 12345, "username": "Vhjxbxbhsbot"}}),
                FakeResponse(
                    {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
                    ok=False,
                    status_code=400,
                ),
            ],
        ) as get:
            with self.assertRaisesRegex(RuntimeError, "check that it is the channel chat ID"):
                fetch_telegram_items()
        self.assertEqual(get.call_count, 4)

    def test_logs_observed_channel_id_mismatch_without_logging_post_text(self):
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "dedicated-test-token",
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        wrong_channel_update = {
            "update_id": 92,
            "channel_post": {
                "message_id": 10,
                "date": 1_750_000_000,
                "chat": {"id": -1009999999999},
                "text": "نص خاص لا يجب أن يظهر في سجل CI",
            },
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse([]),
                FakeResponse({"ok": True, "result": {"url": ""}}),
                FakeResponse({"ok": True, "result": {"id": 12345, "username": "Vhjxbxbhsbot"}}),
                FakeResponse({"ok": True, "result": {"status": "administrator"}}),
                FakeResponse({"ok": True, "result": [wrong_channel_update]}),
            ],
        ), self.assertLogs("telegram_source", level="WARNING") as captured:
            items, cursor = fetch_telegram_items()
        logs = "\n".join(captured.output)
        self.assertEqual(items, [])
        self.assertEqual(cursor, 92)
        self.assertIn("-1009999999999", logs)
        self.assertNotIn("نص خاص لا يجب أن يظهر", logs)

    def test_download_uses_get_file_and_returns_bytes(self):
        token = "test-photo-bot-token"
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": token,
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse({"ok": True, "result": {"file_path": "photos/a.jpg", "file_size": 7}}),
                FakeResponse(chunks=[b"image", b"bytes"]),
            ],
        ) as get:
            result = download_telegram_photo("file-id-1")
        self.assertEqual(result, b"imagebytes")
        self.assertIn("/getFile", get.call_args_list[0].args[0])
        self.assertEqual(get.call_args_list[0].kwargs["params"], {"file_id": "file-id-1"})
        self.assertIn("/file/bot", get.call_args_list[1].args[0])
        self.assertIn(token, get.call_args_list[1].args[0])

    def test_download_rejects_files_above_bot_api_limit(self):
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": "test-token",
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            return_value=FakeResponse({
                "ok": True,
                "result": {"file_path": "photos/large.jpg", "file_size": MAX_TELEGRAM_DOWNLOAD_BYTES + 1},
            }),
        ) as get:
            with self.assertRaisesRegex(RuntimeError, "20 MB"):
                download_telegram_photo("file-id-large")
        self.assertEqual(get.call_count, 1)

    def test_request_error_never_leaks_bot_token(self):
        token = "do-not-leak-this-token"
        env = {
            "TELEGRAM_SOURCE_BOT_TOKEN": token,
            "TELEGRAM_SOURCE_CHAT_ID": "-1001234567890",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "telegram_source.requests.get",
            side_effect=[
                FakeResponse([]),
                FakeResponse({"ok": True, "result": {"url": "", "pending_update_count": 0}}),
                FakeResponse({"ok": True, "result": {"id": 12345, "username": "Vhjxbxbhsbot"}}),
                FakeResponse({"ok": True, "result": {"status": "administrator"}}),
                requests.ConnectionError(f"https://api.telegram.org/bot{token}/getUpdates"),
            ],
        ):
            with self.assertRaises(RuntimeError) as context:
                fetch_telegram_items()
        self.assertNotIn(token, str(context.exception))


if __name__ == "__main__":
    unittest.main()
