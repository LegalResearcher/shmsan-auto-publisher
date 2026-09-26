import os
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

_TEST_TMP = tempfile.TemporaryDirectory(prefix="shmsan-telegram-test-")
_ORIGINAL_CWD = os.getcwd()
os.environ["BOT_DATA_DIR"] = os.path.join(_TEST_TMP.name, "data")
os.environ["SUPABASE_URL"] = "https://example.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "unit-test-service-key"
os.chdir(_TEST_TMP.name)
try:
    import auto_publish_shmsan as publisher
    import shmsan_news_bot as shmsan
finally:
    os.chdir(_ORIGINAL_CWD)


class AutoPublishTelegramTests(unittest.TestCase):
    def _item(self, photo_file_id=None):
        return {
            "title": "عنوان خبر تجريبي",
            "link": "https://t.me/c/4430613399/27",
            "pub_date": datetime(2026, 9, 25, tzinfo=timezone.utc),
            "raw_body": "عنوان خبر تجريبي\n\nالنص الخام الكامل من المنشور.",
            "source_feed": "telegram://-1004430613399",
            "image_url": None,
            "category": "أخبار وتقارير",
            "author": None,
            "_telegram_source": True,
            "_telegram_update_id": 91,
            "_telegram_photo_file_id": photo_file_id,
            "_telegram_video_url": None,
        }

    def _patch_run_dependencies(self, stack, item, rewrite_side_effect=None):
        mocked = {}
        def p(name, **kwargs):
            mocked[name] = stack.enter_context(patch.object(publisher, name, **kwargs))
            return mocked[name]

        p("check_system_logs_size")
        p("check_and_notify_scheduled_posts")
        p("get_existing_source_urls", return_value=set())
        p("load_blocked_links", return_value=set())
        p("get_recent_published_titles", return_value=[])
        p("get_recent_published_titles_from_db", return_value=[])
        p("collect_recent_items", return_value=[])
        p("is_telegram_source_configured", return_value=True)
        p("fetch_telegram_items", return_value=([item], 91))
        p("merge_photo_replies_with_news_items", side_effect=lambda items, existing_source_urls: (items, []))
        p("remove_duplicate_news", side_effect=lambda items, history_items, duplicates_out=None: items)
        p("apply_full_extraction")
        p("rewrite_article", side_effect=rewrite_side_effect or None, return_value={
            "title": "عنوان محرر",
            "excerpt": "ملخص محرر",
            "content": "متن محرر كامل.",
        })
        p("check_similar_published_title_db", return_value=None)
        p("get_category_id", return_value="category-id")
        p("get_post_image_url", return_value=("https://storage.example/photo.webp", None))
        p("format_content_paragraphs", return_value="<p>متن محرر كامل.</p>")
        p("word_stats", return_value=(4, 1))
        p("extract_keywords", return_value=["خبر"])
        p("make_slug", return_value="news-slug")
        p("generate_meta_title", return_value="SEO title")
        p("generate_meta_description", return_value="SEO description")
        p("sb_insert", return_value="post-id")
        p("get_published_post_by_title", return_value={
            "id": "existing-post-id",
            "title": "عنوان الخبر المنشور سابقاً",
            "external_video_url": None,
        })
        p("update_published_post_video_url", return_value=True)
        p("update_published_post_cover_image", return_value=True)
        p("log_published_title")
        p("save_published_title_to_db")
        p("save_blocked_link")
        p("seed_views")
        p("build_canonical_url", return_value="https://shmsan.example/news-slug")
        p("send_to_telegram", return_value=True)
        p("log_discovery_ready")
        p("commit_telegram_cursor")
        return mocked

    def test_telegram_post_bypasses_page_extractor_and_uses_shamsan_rewriter(self):
        with ExitStack() as stack:
            mocked = self._patch_run_dependencies(stack, self._item())
            publisher.run()

        mocked["apply_full_extraction"].assert_not_called()
        mocked["rewrite_article"].assert_called_once_with(
            "عنوان خبر تجريبي",
            "عنوان خبر تجريبي\n\nالنص الخام الكامل من المنشور.",
            "أخبار وتقارير",
            bypass_houthi_iran_filter=True,
            bypass_content_filters=True,
            video_url=None,
        )
        mocked["sb_insert"].assert_called_once()
        mocked["commit_telegram_cursor"].assert_called_once_with(91)

    def test_telegram_photo_bytes_use_shamsan_image_processor(self):
        with ExitStack() as stack:
            mocked = self._patch_run_dependencies(stack, self._item("telegram-file-id"))
            mocked["download_telegram_photo"] = stack.enter_context(
                patch.object(publisher, "download_telegram_photo", return_value=b"telegram-photo-bytes")
            )
            publisher.run()

        mocked["download_telegram_photo"].assert_called_once_with("telegram-file-id")
        mocked["get_post_image_url"].assert_called_once_with(
            None,
            headline_text="عنوان محرر",
            article_url="https://t.me/c/4430613399/27",
            source_image_bytes=b"telegram-photo-bytes",
        )
        record = mocked["sb_insert"].call_args.args[0]
        self.assertEqual(record["cover_image"], "https://storage.example/photo.webp")

    def test_telegram_video_url_is_saved_in_external_video_field(self):
        item = self._item()
        item["_telegram_video_url"] = "https://youtu.be/video123"
        with ExitStack() as stack:
            mocked = self._patch_run_dependencies(stack, item)
            publisher.run()
        self.assertTrue(mocked["rewrite_article"].call_args.kwargs["bypass_houthi_iran_filter"])
        self.assertTrue(mocked["rewrite_article"].call_args.kwargs["bypass_content_filters"])
        record = mocked["sb_insert"].call_args.args[0]
        self.assertEqual(record["external_video_url"], "https://youtu.be/video123")

    def test_telegram_video_url_is_removed_from_editorial_text(self):
        video_url = "https://x.com/example/status/123"
        model_result = {
            "title": "عنوان محرر",
            "excerpt": f"ملخص الخبر {video_url}",
            "content": f"متن الخبر.\n\nيمكن متابعة التفاصيل هنا: {video_url}",
            "houthi_iran_exclude": False,
        }
        with patch.object(shmsan, "call_with_rotation", return_value=json.dumps(model_result)):
            result = publisher.rewrite_article(
                "عنوان المصدر",
                "نص المصدر",
                "أخبار وتقارير",
                bypass_houthi_iran_filter=True,
                bypass_content_filters=True,
                video_url=video_url,
            )
        self.assertNotIn(video_url, result["excerpt"])
        self.assertNotIn(video_url, result["content"])

    def test_processing_failure_does_not_commit_telegram_cursor(self):
        with ExitStack() as stack:
            mocked = self._patch_run_dependencies(
                stack,
                self._item(),
                rewrite_side_effect=RuntimeError("simulated rewrite failure"),
            )
            with self.assertRaisesRegex(RuntimeError, "فشل نشر جميع الأخبار"):
                publisher.run()

        mocked["commit_telegram_cursor"].assert_not_called()
        mocked["sb_insert"].assert_not_called()

    def test_duplicate_telegram_video_updates_published_post_without_republishing(self):
        item = self._item()
        item["_telegram_video_url"] = "https://x.com/source/status/12345"

        def mark_duplicate(items, history_items, duplicates_out=None):
            if duplicates_out is not None:
                item["_duplicate_match_title"] = "عنوان الخبر المنشور سابقاً"
                duplicates_out.append(item)
            return []

        with ExitStack() as stack:
            mocked = self._patch_run_dependencies(stack, item)
            mocked["remove_duplicate_news"].side_effect = mark_duplicate
            publisher.run()

        mocked["get_published_post_by_title"].assert_called_once_with("عنوان الخبر المنشور سابقاً")
        mocked["update_published_post_video_url"].assert_called_once_with(
            "existing-post-id", "https://x.com/source/status/12345"
        )
        mocked["sb_insert"].assert_not_called()
        mocked["commit_telegram_cursor"].assert_called_once_with(91)

    def test_late_database_duplicate_check_also_keeps_telegram_video(self):
        item = self._item()
        item["_telegram_video_url"] = "https://x.com/source/status/12345"
        with ExitStack() as stack:
            mocked = self._patch_run_dependencies(stack, item)
            mocked["check_similar_published_title_db"].return_value = {
                "title": "عنوان الخبر المنشور سابقاً",
                "similarity_score": 0.96,
            }
            publisher.run()

        mocked["get_published_post_by_title"].assert_called_once_with("عنوان الخبر المنشور سابقاً")
        mocked["update_published_post_video_url"].assert_called_once_with(
            "existing-post-id", "https://x.com/source/status/12345"
        )
        mocked["sb_insert"].assert_not_called()

    def test_title_dedup_transfers_media_to_kept_item_in_same_batch(self):
        from datetime import datetime, timezone

        primary = self._item()
        primary.update({"_telegram_source": False, "_telegram_video_url": None})
        duplicate = self._item()
        duplicate["title"] = "عنوان مصاغ بطريقة أخرى"
        duplicate["_telegram_video_url"] = "https://x.com/source/status/12345"
        primary["pub_date"] = duplicate["pub_date"] = datetime.now(timezone.utc)
        with (
            patch.object(shmsan, "get_title_embedding", return_value=[1.0, 0.0]),
            patch.object(shmsan, "_cosine_similarity", return_value=0.99),
        ):
            kept = shmsan.remove_duplicate_news([primary, duplicate], history_items=[])

        self.assertEqual(kept, [primary])
        self.assertEqual(primary["_telegram_video_url"], "https://x.com/source/status/12345")

    def test_title_dedup_returns_historical_match_for_media_attachment(self):
        from datetime import datetime, timezone

        item = self._item()
        item["_telegram_video_url"] = "https://x.com/source/status/12345"
        item["pub_date"] = datetime.now(timezone.utc)
        duplicates = []
        history = [{
            "title": "العنوان المنشور سابقاً",
            "pub_date": item["pub_date"],
            "embedding": [1.0, 0.0],
        }]
        with (
            patch.object(shmsan, "get_title_embedding", return_value=[1.0, 0.0]),
            patch.object(shmsan, "_cosine_similarity", return_value=0.99),
        ):
            kept = shmsan.remove_duplicate_news([item], history_items=history, duplicates_out=duplicates)

        self.assertEqual(kept, [])
        self.assertEqual(duplicates[0]["_duplicate_match_title"], "العنوان المنشور سابقاً")


if __name__ == "__main__":
    unittest.main()
