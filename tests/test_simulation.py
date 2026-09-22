"""Executable simulation tests for the Codex Quota MCP."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server  # noqa: E402


FAKE_COMMAND = str(Path(__file__).with_name("fake_codex.cmd"))


class CodexQuotaSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_env = os.environ.copy()
        os.environ["CODEX_COMMAND"] = FAKE_COMMAND
        os.environ["CODEX_QUOTA_STATE"] = str(Path(self.temp_dir.name) / "history.json")
        os.environ["FAKE_CODEX_MODE"] = "normal"
        os.environ["FAKE_PRIMARY_USED"] = "25"
        os.environ["FAKE_SECONDARY_USED"] = "40"

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp_dir.cleanup()

    def test_all_modes_are_declared_and_select_the_expected_window(self) -> None:
        self.assertEqual(server.TOOLS[0]["inputSchema"]["properties"]["mode"]["enum"], ["5m", "15m", "30m", "task_start"])
        expected = {"5m": ["5m"], "15m": ["15m"], "30m": ["30m"], "task_start": ["5m", "15m", "30m"]}
        for mode, windows in expected.items():
            os.environ["CODEX_QUOTA_STATE"] = str(Path(self.temp_dir.name) / f"{mode}.json")
            result = server.get_quota({"mode": mode})
            self.assertEqual(list(result["brief_report"]["resource_brief"]["observed_consumption"]), windows)

    def test_sliding_burn_reports_partial_observation_honestly(self) -> None:
        now = 1_000_000.0
        observations = [
            {"timestamp": now - 240, "primary_used_percent": 10},
            {"timestamp": now - 100, "primary_used_percent": 12},
        ]
        current = {"timestamp": now, "primary_used_percent": 13}
        result = server.sliding_burn(observations + [current], current, 5)
        self.assertEqual(result["consumed_percent"], 3)
        self.assertEqual(result["observed_minutes"], 4.0)
        self.assertEqual(result["sample_count"], 3)

    def test_task_start_bypasses_normal_report_gate(self) -> None:
        state = str(Path(self.temp_dir.name) / "gate.json")
        os.environ["CODEX_QUOTA_STATE"] = state
        first = server.get_quota({"mode": "task_start"})
        second = server.get_quota({"mode": "15m"})
        self.assertTrue(first["report_due"])
        self.assertFalse(second["report_due"])
        self.assertNotIn("brief_report", second)

    def test_app_server_error_includes_stderr_and_returns_quickly(self) -> None:
        os.environ["FAKE_CODEX_MODE"] = "stderr"
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "simulated app-server diagnostic"):
            server.app_server_request("account/rateLimits/read")
        self.assertLess(time.monotonic() - started, 3)

    def test_app_server_timeout_is_bounded(self) -> None:
        os.environ["FAKE_CODEX_MODE"] = "hang"
        os.environ["CODEX_APP_SERVER_TIMEOUT_SECONDS"] = "0.3"
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "within 0.3s"):
            server.app_server_request("account/rateLimits/read")
        self.assertLess(time.monotonic() - started, 3)

    def test_history_is_valid_and_bounded_after_repeated_writes(self) -> None:
        for _ in range(server.MAX_OBSERVATIONS + 10):
            server.get_quota({"mode": "15m"})
        history = json.loads(Path(os.environ["CODEX_QUOTA_STATE"]).read_text(encoding="utf-8"))
        self.assertLessEqual(len(history["observations"]), server.MAX_OBSERVATIONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)

