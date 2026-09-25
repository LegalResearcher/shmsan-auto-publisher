import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

_TEST_TMP = tempfile.TemporaryDirectory(prefix="shmsan-gemini-rotation-")
_ORIGINAL_CWD = os.getcwd()
os.environ["BOT_DATA_DIR"] = os.path.join(_TEST_TMP.name, "data")
os.environ["SUPABASE_URL"] = "https://example.invalid"
os.environ["SUPABASE_SERVICE_KEY"] = "unit-test-service-key"
os.chdir(_TEST_TMP.name)
try:
    import shmsan_news_bot as shmsan
finally:
    os.chdir(_ORIGINAL_CWD)


class GeminiRotationParityTests(unittest.TestCase):
    def test_model_cascades_match_janoub(self):
        self.assertEqual(
            shmsan.DAY_MODEL_CASCADE,
            [
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3.7-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.1-flash-lite",
            ],
        )
        self.assertEqual(
            shmsan.NIGHT_MODEL_CASCADE,
            ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
        )

    def test_loads_grouped_keys_and_legacy_flat_keys(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY_GROUPS": "a1,a2; b1 ; ;b2,b3", "GEMINI_API_KEYS": "legacy1,legacy2"},
        ):
            self.assertEqual(
                shmsan._load_gemini_key_groups(),
                [["a1", "a2"], ["b1"], ["b2", "b3"]],
            )
        with patch.object(shmsan, "GEMINI_API_KEYS", ["legacy1", "legacy2"]), patch.dict(
            os.environ,
            {"GEMINI_API_KEY_GROUPS": "", "GEMINI_API_KEYS": "legacy1, legacy2"},
        ):
            self.assertEqual(shmsan._load_gemini_key_groups(), [["legacy1", "legacy2"]])

    def test_night_mode_boundary_matches_yemen_midnight_to_1359(self):
        self.assertTrue(shmsan.is_night_mode(datetime(2026, 9, 25, 13, 59)))
        self.assertFalse(shmsan.is_night_mode(datetime(2026, 9, 25, 14, 0)))
        self.assertFalse(
            shmsan.is_night_mode(datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc))
        )

    def test_day_rotation_tries_all_models_before_next_key_and_group(self):
        groups = [["a1", "a2"], ["b1"]]
        models = ["m1", "m2"]
        sequence = []

        def fake_call(*_args, **_kwargs):
            sequence.append((shmsan.current_model(), shmsan.current_key()))
            if len(sequence) < 5:
                raise shmsan.DailyQuotaExceeded()
            return "success"

        with (
            patch.object(shmsan, "KEY_GROUPS", groups),
            patch.object(shmsan, "MODEL_CASCADE", models),
            patch.object(shmsan, "NIGHT_MODE", False),
            patch.object(shmsan, "_current_group_idx", 0),
            patch.object(shmsan, "_current_key_idx", 0),
            patch.object(shmsan, "_model_stage_idx", 0),
            patch.object(shmsan, "call_gemini", side_effect=fake_call),
        ):
            result = shmsan.call_with_rotation("test")

        self.assertEqual(result, "success")
        self.assertEqual(
            sequence,
            [
                ("m1", "a1"),
                ("m2", "a1"),
                ("m1", "a2"),
                ("m2", "a2"),
                ("m1", "b1"),
            ],
        )

    def test_night_rotation_tries_models_then_keys_and_groups_in_reverse(self):
        groups = [["a1", "a2"], ["b1", "b2"]]
        models = ["lite1", "lite2"]
        sequence = []

        def fake_call(*_args, **_kwargs):
            sequence.append((shmsan.current_model(), shmsan.current_key()))
            if len(sequence) < 5:
                raise shmsan.DailyQuotaExceeded()
            return "success"

        with (
            patch.object(shmsan, "KEY_GROUPS", groups),
            patch.object(shmsan, "MODEL_CASCADE", models),
            patch.object(shmsan, "NIGHT_MODE", True),
            patch.object(shmsan, "_current_group_idx", 1),
            patch.object(shmsan, "_current_key_idx", 1),
            patch.object(shmsan, "_model_stage_idx", 0),
            patch.object(shmsan, "call_gemini", side_effect=fake_call),
        ):
            result = shmsan.call_with_rotation("test")

        self.assertEqual(result, "success")
        self.assertEqual(
            sequence,
            [
                ("lite1", "b2"),
                ("lite2", "b2"),
                ("lite1", "b1"),
                ("lite2", "b1"),
                ("lite1", "a2"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
