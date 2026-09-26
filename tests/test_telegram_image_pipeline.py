import os
import tempfile
import unittest
from unittest.mock import patch

# shmsan_news_bot requires Supabase credentials at import time and initializes
# log/data paths. Use inert test-only values and a temporary working directory.
_TEST_TMP = tempfile.TemporaryDirectory(prefix="janoub-image-test-")
_ORIGINAL_CWD = os.getcwd()
os.environ["BOT_DATA_DIR"] = os.path.join(_TEST_TMP.name, "data")
os.environ["SUPABASE_URL"] = "https://example.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "unit-test-service-key"
os.chdir(_TEST_TMP.name)
try:
    from shmsan_news_bot import get_post_image_url
    import shmsan_news_bot as shmsan
    import auto_publish_shmsan as publisher
finally:
    os.chdir(_ORIGINAL_CWD)


class TelegramImagePipelineTests(unittest.TestCase):
    def test_source_bytes_use_existing_processing_and_storage_pipeline(self):
        raw = b"fake-telegram-photo-bytes"
        with (
            patch.object(shmsan, "HEADLINE_DESIGN_ENABLED", False),
            patch.object(shmsan, "image_contains_blocked_logo", return_value=False),
            patch.object(shmsan, "compress_image_to_webp", return_value=b"processed-webp") as compress,
            patch.object(shmsan, "upload_image_to_supabase", return_value="https://cdn.example/photo.webp") as upload,
            patch.object(shmsan, "download_image_bytes") as download_url,
            patch.object(shmsan, "fetch_og_image") as fetch_og,
        ):
            image_url, square_url = get_post_image_url(
                None,
                article_url="https://t.me/c/4430613399/12",
                headline_text="عنوان الخبر",
                source_image_bytes=raw,
            )
        self.assertEqual(image_url, "https://cdn.example/photo.webp")
        self.assertIsNone(square_url)
        compress.assert_called_once_with(raw)
        upload.assert_called_once()
        download_url.assert_not_called()
        fetch_og.assert_not_called()

    def test_late_reply_updates_published_cover(self):
        reply = {"link": "https://t.me/c/4430613399/27", "_telegram_photo_file_id": "late-photo"}
        with (
            patch.object(publisher, "get_published_post_by_source_url", return_value={"id": "post-id", "title": "عنوان الخبر"}),
            patch.object(publisher, "download_telegram_photo", return_value=b"photo"),
            patch.object(publisher, "get_post_image_url", return_value=("https://cdn.example/cover.webp", None)),
            patch.object(publisher, "update_published_post_cover_image", return_value=True) as update_cover,
        ):
            retry = publisher._process_late_telegram_photo_replies([reply])
        self.assertFalse(retry)
        update_cover.assert_called_once_with("post-id", "https://cdn.example/cover.webp")

    def test_late_reply_cover_failure_preserves_retry(self):
        reply = {"link": "https://t.me/c/4430613399/27", "_telegram_photo_file_id": "late-photo"}
        with (
            patch.object(publisher, "get_published_post_by_source_url", return_value={"id": "post-id", "title": "عنوان الخبر"}),
            patch.object(publisher, "download_telegram_photo", return_value=b"photo"),
            patch.object(publisher, "get_post_image_url", return_value=("https://cdn.example/cover.webp", None)),
            patch.object(publisher, "update_published_post_cover_image", return_value=False),
        ):
            retry = publisher._process_late_telegram_photo_replies([reply])
        self.assertTrue(retry)


if __name__ == "__main__":
    unittest.main()
