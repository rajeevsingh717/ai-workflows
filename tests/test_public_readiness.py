import unittest
from pathlib import Path

from generate_launchd_plist import build_plist
from memory_store.ingest.notion import _property_value
from memory_store.ingest.pdf import is_scanned
from transaction_store import _row_key


class LaunchdGenerationTests(unittest.TestCase):
    def test_market_schedule_is_monday_through_friday(self):
        plist = build_plist("market-check", Path("/tmp/ai-workflows"))
        weekdays = {item["Weekday"] for item in plist["StartCalendarInterval"]}
        self.assertEqual(weekdays, {2, 3, 4, 5, 6})
        self.assertEqual(len(plist["StartCalendarInterval"]), 15)

    def test_generated_paths_follow_checkout(self):
        root = Path("/tmp/example checkout").resolve()
        plist = build_plist("telegram-listener", root)
        self.assertEqual(plist["WorkingDirectory"], str(root))
        self.assertIn(str(root / "telegram_listener.py"), plist["ProgramArguments"])


class IngestionHelperTests(unittest.TestCase):
    def test_notion_rich_text(self):
        prop = {"type": "rich_text", "rich_text": [{"plain_text": "Hello"}, {"plain_text": " world"}]}
        self.assertEqual(_property_value(prop), "Hello world")

    def test_scanned_pdf_detection(self):
        self.assertTrue(is_scanned("short"))
        self.assertFalse(is_scanned("word " * 100))


class TransactionDedupTests(unittest.TestCase):
    def test_description_drift_does_not_change_identity(self):
        base = {
            "Run Date": "01/02/2026",
            "Account Number": "123",
            "Action": "YOU BOUGHT SAMPLE",
            "Symbol": "SAMPLE",
            "Price ($)": "10.00",
            "Quantity": "2",
            "Amount ($)": "-20.00",
            "Settlement Date": "01/05/2026",
            "Description": "Old description",
        }
        changed = dict(base, Description="New description")
        self.assertEqual(_row_key(base), _row_key(changed))


if __name__ == "__main__":
    unittest.main()
