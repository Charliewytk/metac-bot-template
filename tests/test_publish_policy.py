"""Publish-policy and dry-run network rails. Never hits Metaculus."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from bot_helpers import parse_bot_cli
from forecast_guard import (
    SubmitBlocked,
    assert_submit_allowed,
    blocked_write_log,
    env_forces_dry_run,
    install_dry_run_network_guard,
    is_metaculus_write,
    resolve_publish_intent,
    set_publish_allowed,
    uninstall_dry_run_network_guard,
)
from dry_run import main as dry_run_main


class TestResolvePublishIntent(unittest.TestCase):
    def test_default_is_dry_run(self) -> None:
        self.assertFalse(resolve_publish_intent(environ={}))

    def test_submit_opt_in(self) -> None:
        self.assertTrue(resolve_publish_intent(submit=True, environ={}))

    def test_dry_run_flag_beats_submit(self) -> None:
        self.assertFalse(resolve_publish_intent(submit=True, dry_run=True, environ={}))

    def test_env_beats_submit(self) -> None:
        for value in ("1", "true", "YES", "on"):
            self.assertFalse(
                resolve_publish_intent(
                    submit=True, environ={"METACULUS_DRY_RUN": value}
                ),
                msg=value,
            )
        self.assertTrue(
            resolve_publish_intent(submit=True, environ={"METACULUS_DRY_RUN": "0"})
        )

    def test_env_forces_dry_run_helper(self) -> None:
        self.assertTrue(env_forces_dry_run({"METACULUS_DRY_RUN": "true"}))
        self.assertFalse(env_forces_dry_run({"METACULUS_DRY_RUN": ""}))
        self.assertFalse(env_forces_dry_run({}))


class TestCli(unittest.TestCase):
    def test_default_cli_is_not_submit(self) -> None:
        args = parse_bot_cli([])
        self.assertFalse(args.submit)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.force_reforecast)
        self.assertEqual(args.mode, "tournament")
        self.assertFalse(resolve_publish_intent(submit=args.submit, dry_run=args.dry_run))

    def test_submit_and_force_flags(self) -> None:
        args = parse_bot_cli(["--mode", "metaculus_cup", "--submit", "--force-reforecast"])
        self.assertTrue(args.submit)
        self.assertTrue(args.force_reforecast)
        self.assertEqual(args.mode, "metaculus_cup")

    def test_submit_and_dry_run_are_mutually_exclusive(self) -> None:
        stderr = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(stderr):
            parse_bot_cli(["--submit", "--dry-run"])


class TestSubmitLatch(unittest.TestCase):
    def tearDown(self) -> None:
        set_publish_allowed(False)

    def test_assert_blocks_when_latched_shut(self) -> None:
        set_publish_allowed(False)
        with self.assertRaises(SubmitBlocked):
            assert_submit_allowed("https://www.metaculus.com/api/questions/forecast/")

    def test_assert_allows_when_latched_open(self) -> None:
        set_publish_allowed(True)
        assert_submit_allowed("https://www.metaculus.com/api/questions/forecast/")


class TestNetworkGuard(unittest.TestCase):
    def setUp(self) -> None:
        set_publish_allowed(False)
        uninstall_dry_run_network_guard()

    def tearDown(self) -> None:
        set_publish_allowed(False)
        uninstall_dry_run_network_guard()

    def test_classifies_metaculus_writes(self) -> None:
        self.assertTrue(
            is_metaculus_write(
                "POST", "https://www.metaculus.com/api/questions/forecast/"
            )
        )
        self.assertTrue(
            is_metaculus_write(
                "POST", "https://www.metaculus.com/api/comments/create/"
            )
        )
        self.assertFalse(
            is_metaculus_write("GET", "https://www.metaculus.com/api/posts/")
        )
        self.assertFalse(
            is_metaculus_write("POST", "https://openrouter.ai/api/v1/chat/completions")
        )

    def test_install_blocks_forecast_post(self) -> None:
        try:
            import requests
        except ImportError:
            self.skipTest("requests not installed")

        install_dry_run_network_guard()
        with self.assertRaises(SubmitBlocked):
            requests.post(
                "https://www.metaculus.com/api/questions/forecast/",
                json=[{"question": 1}],
                timeout=1,
            )
        self.assertTrue(any("forecast" in entry for entry in blocked_write_log()))

    def test_get_is_not_classified_as_a_write(self) -> None:
        self.assertFalse(
            is_metaculus_write("GET", "https://www.metaculus.com/api/posts/")
        )
        self.assertFalse(
            is_metaculus_write("HEAD", "https://www.metaculus.com/api/questions/forecast/")
        )


class TestOfflineDryRun(unittest.TestCase):
    def test_dry_run_script_exits_zero_and_never_publishes(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = dry_run_main([])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("publish=no", out)
        self.assertIn("Second pass", out)
        self.assertIn("no-op", out.lower())
        self.assertNotIn("submit=yes", out)


if __name__ == "__main__":
    unittest.main()
