"""Source-level rails: default is dry-run; Actions still pass --submit."""
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestSubmitDefaults(unittest.TestCase):
    def test_main_py_does_not_hardcode_publish_true(self) -> None:
        text = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("publish_to_metaculus = True", text)
        self.assertIn("resolve_publish_intent", text)
        self.assertIn("install_dry_run_network_guard", text)
        self.assertNotIn(
            "skip_previously_forecasted_questions = False",
            text,
        )

    def test_no_framework_defaults_to_no_submit(self) -> None:
        text = (ROOT / "main_with_no_framework.py").read_text(encoding="utf-8")
        self.assertIn("SUBMIT_PREDICTION = False", text)
        self.assertNotIn("SUBMIT_PREDICTION = True", text)
        self.assertIn("assert_submit_allowed", text)

    def test_forecast_workflows_pass_submit(self) -> None:
        for name in (
            "run_bot_on_tournament.yaml",
            "run_bot_on_metaculus_cup.yaml",
            "test_bot.yaml",
        ):
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn("--submit", text, msg=name)

    def test_unit_test_and_review_workflows_do_not_submit(self) -> None:
        for name in ("unit_tests.yaml", "review_bot.yaml"):
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertNotIn("--submit", text, msg=name)
            self.assertNotIn("python main.py", text, msg=name)


if __name__ == "__main__":
    unittest.main()
