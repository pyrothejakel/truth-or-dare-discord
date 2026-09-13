import tempfile
import unittest
from pathlib import Path

from truth_or_dare_bot.storage import Store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_defaults_and_settings(self):
        self.assertIsNotNone(self.store.choose_prompt())
        self.assertFalse(self.store.paused())
        self.store.set_setting("paused", "true")
        self.assertTrue(self.store.paused())

    def test_filter_and_history(self):
        self.store.add_prompts([("truth", 5, "deep", "Test prompt")])
        prompt = self.store.choose_prompt("truth", "deep", 5)
        self.assertEqual(prompt.text, "Test prompt")
        self.store.record_post(prompt.id)
        self.assertEqual(self.store.choose_prompt("truth", "deep", 5).text, "Test prompt")

if __name__ == "__main__":
    unittest.main()

