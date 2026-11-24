"""Proves --output-prefix pass2 never touches pass1.json.

No real LLM/network calls: Composio, the LLM, and tavily_search are all
mocked. Run with: python tests/test_output_prefix.py
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import research_agent.run as run_mod  # noqa: E402

PASS1_PATH = Path("output/pass1.json")


class FakeLLM:
    provider = "gemini"
    model_name = "fake-gemini-model"
    requests_today = 7

    def invoke_structured(self, schema, messages):
        # Only ever called for QueryPlan here: with no search results,
        # fetch_pages returns no pages, and extract's "not pages" branch
        # builds its draft directly without calling the LLM again.
        return schema(queries=["q1", "q2", "q3"]), self.provider, self.model_name


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestOutputPrefixNeverTouchesPass1(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PASS1_PATH.exists(), "output/pass1.json must exist for this test to be meaningful")
        self.original_mode = PASS1_PATH.stat().st_mode
        self.original_hash = _sha256(PASS1_PATH)

        # Read-only: any open(pass1.json, "w") (direct, or via os.replace's
        # temp-file rename target) would raise PermissionError immediately.
        os.chmod(PASS1_PATH, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

        self.test_pass_path = Path("output/pass2.json")
        self.test_failures_path = Path("output/pass2_failures.json")
        for p in (self.test_pass_path, self.test_failures_path):
            if p.exists():
                p.unlink()

    def tearDown(self):
        os.chmod(PASS1_PATH, self.original_mode)
        self.assertEqual(_sha256(PASS1_PATH), self.original_hash, "pass1.json content changed during the test!")
        for p in (self.test_pass_path, self.test_failures_path):
            if p.exists():
                p.unlink()

    def test_pass2_run_does_not_open_pass1_for_writing(self):
        args = SimpleNamespace(
            apps="HubSpot",
            apps_file=None,
            only=None,
            all=False,
            limit=None,
            no_cache=True,
            output_prefix="pass2",
            gemini_used_today=0,
        )

        with patch("research_agent.run.Composio", return_value=MagicMock()), \
             patch("research_agent.run.build_extraction_llm", return_value=FakeLLM()), \
             patch("research_agent.graph.tavily_search", return_value={"results": []}), \
             patch.dict(os.environ, {"LLM_PROVIDER": "gemini"}):
            # Must not raise PermissionError (which it would if any code
            # path tried to write pass1.json while it's chmod'd read-only).
            run_mod._main(args)

        self.assertTrue(self.test_pass_path.exists(), "output/pass2.json should have been written")
        pass2_data = json.loads(self.test_pass_path.read_text())
        self.assertEqual(len(pass2_data), 1)
        record = next(iter(pass2_data.values()))
        self.assertEqual(record["name"], "HubSpot")

    def test_pass2_skip_check_ignores_pass1_completed_apps(self):
        # HubSpot is already done in pass1.json (confirmed by a prior real
        # run). A pass2 run for the same app must NOT skip it just because
        # pass1 already has it - skip-checking must look only at pass2.json.
        pass1_data = json.loads(PASS1_PATH.read_text())
        hubspot_done_in_pass1 = any(r.get("name") == "HubSpot" for r in pass1_data.values())
        self.assertTrue(hubspot_done_in_pass1, "expected HubSpot to already be in pass1.json for this test to prove anything")

        args = SimpleNamespace(
            apps="HubSpot",
            apps_file=None,
            only=None,
            all=False,
            limit=None,
            no_cache=True,
            output_prefix="pass2",
            gemini_used_today=0,
        )

        with patch("research_agent.run.Composio", return_value=MagicMock()), \
             patch("research_agent.run.build_extraction_llm", return_value=FakeLLM()), \
             patch("research_agent.graph.tavily_search", return_value={"results": []}), \
             patch.dict(os.environ, {"LLM_PROVIDER": "gemini"}):
            run_mod._main(args)

        pass2_data = json.loads(self.test_pass_path.read_text())
        self.assertEqual(len(pass2_data), 1, "HubSpot should have been processed under pass2, not skipped because of pass1")


if __name__ == "__main__":
    unittest.main()
