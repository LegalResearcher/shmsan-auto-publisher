import os
import tempfile
import unittest
from datetime import datetime, timezone

_TEST_TMP = tempfile.TemporaryDirectory(prefix="shmsan-rss-date-")
_ORIGINAL_CWD = os.getcwd()
os.environ["BOT_DATA_DIR"] = os.path.join(_TEST_TMP.name, "data")
os.environ["SUPABASE_URL"] = "https://example.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "unit-test-service-key"
os.chdir(_TEST_TMP.name)
try:
    import shmsan_news_bot as shmsan
finally:
    os.chdir(_ORIGINAL_CWD)


class RSSDateTimezoneTests(unittest.TestCase):
    def test_4may_gmt_wall_clock_is_reinterpreted_as_aden_local(self):
        parsed = shmsan.parse_pub_date(
            "Fri, 02 Oct 2026 16:08:35 GMT",
            source_url=shmsan.RSS_4MAY_FULL_URL,
        )

        self.assertEqual(
            parsed,
            datetime(2026, 10, 2, 13, 8, 35, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parsed.astimezone(shmsan.YEMEN_TZ).isoformat(),
            "2026-10-02T16:08:35+03:00",
        )

    def test_other_feed_gmt_timestamp_remains_utc(self):
        parsed = shmsan.parse_pub_date(
            "Fri, 02 Oct 2026 16:08:35 GMT",
            source_url=shmsan.RSS_ADEN_TM_FULL_URL,
        )

        self.assertEqual(
            parsed,
            datetime(2026, 10, 2, 16, 8, 35, tzinfo=timezone.utc),
        )

    def test_4may_explicit_aden_offset_is_not_shifted_twice(self):
        parsed = shmsan.parse_pub_date(
            "2026-10-02T16:08:35+03:00",
            source_url=shmsan.RSS_4MAY_FULL_URL,
        )

        self.assertEqual(parsed.isoformat(), "2026-10-02T16:08:35+03:00")
