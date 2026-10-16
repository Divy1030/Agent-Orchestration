import unittest
from datetime import datetime, timezone

from backend.agent_executor import _relative_date_guidance


class RelativeDateGuidanceTests(unittest.TestCase):
    def test_guidance_anchors_inclusive_last_days_to_current_date(self):
        guidance = _relative_date_guidance(
            datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc), "UTC"
        )
        self.assertIn("Current date: 2026-10-15 (UTC)", guidance)
        self.assertIn("N calendar dates ending today, inclusive", guidance)
        self.assertIn("start=today minus N-1 days, end=today", guidance)
        self.assertIn("Never invent dates or results", guidance)

    def test_timezone_is_used_for_local_calendar_date(self):
        guidance = _relative_date_guidance(
            datetime(2026, 10, 14, 20, 0, tzinfo=timezone.utc), "Asia/Kolkata"
        )
        self.assertIn("Current date: 2026-10-15 (Asia/Kolkata)", guidance)


if __name__ == "__main__":
    unittest.main()
