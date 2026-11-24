"""Proves evaluate.py works offline for pass 2 and never writes to pass1.json.

No LLM/network calls - this only reads JSON files and does arithmetic.
Run with: python tests/test_evaluate_compare.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import research_agent.evaluate as ev  # noqa: E402

PASS1_PATH = Path("output/pass1.json")
EVAL_PASS1_PATH = Path("output/eval_pass1.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestEvaluateReadOnly(unittest.TestCase):
    """--output-prefix pass1 (and --compare pass1 ...) must never write to
    pass1.json or eval_pass1.json. Proven purely by a sha256 hash taken
    before and after each test - not by chmod/PermissionError, which this
    sandbox doesn't reliably enforce (confirmed: a chmod 444 file was
    written to successfully in an earlier session)."""

    def setUp(self):
        self.assertTrue(PASS1_PATH.exists(), "output/pass1.json must exist for this test to be meaningful")
        self.assertTrue(EVAL_PASS1_PATH.exists(), "output/eval_pass1.json must exist for this test to be meaningful")
        self.pass1_hash_before = _sha256(PASS1_PATH)
        self.eval_pass1_hash_before = _sha256(EVAL_PASS1_PATH)

    def tearDown(self):
        self.assertEqual(_sha256(PASS1_PATH), self.pass1_hash_before, "pass1.json content changed during the test!")
        self.assertEqual(
            _sha256(EVAL_PASS1_PATH), self.eval_pass1_hash_before, "eval_pass1.json content changed during the test!"
        )

    def test_single_mode_does_not_write_pass1(self):
        # run_single writes its eval report to eval_out_path(prefix) by
        # design - that's fine, it's just not output/eval_pass1.json for
        # this test. Redirect it into a temp folder so nothing real ever
        # gets touched, regardless of what this test does.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_eval_out = Path(tmpdir) / "eval_pass1.json"
            with patch("research_agent.evaluate.eval_out_path", return_value=tmp_eval_out):
                ev.run_single("pass1")
            self.assertTrue(tmp_eval_out.exists())

    def test_compare_mode_does_not_write_pass1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_base = Path(tmpdir)
            shutil.copy(PASS1_PATH, tmp_base / "pass2.json")

            gold_rows = ev._load_gold()
            records1 = json.loads(PASS1_PATH.read_text())
            records2 = ev._load_records(ev.record_path("pass2", base_dir=tmp_base))
            common_ids = {g["id"] for g in gold_rows if g["id"] in records1 and g["id"] in records2}
            self.assertEqual(len(common_ids), 20, "fake pass2 is a copy of pass1, so all 20 gold apps should be in both")


class TestFakePass2IsIdentical(unittest.TestCase):
    """A pass2.json that's a byte-for-byte copy of pass1.json, in its own
    temp folder, must score identically to pass1 on every field - every
    change should be exactly 0."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_base = Path(self.tmpdir.name)
        shutil.copy(PASS1_PATH, self.tmp_base / "pass1.json")
        shutil.copy(PASS1_PATH, self.tmp_base / "pass2.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_compare_identical_files_has_zero_change_everywhere(self):
        gold_rows = ev._load_gold()
        records1 = ev._load_records(ev.record_path("pass1", base_dir=self.tmp_base))
        records2 = ev._load_records(ev.record_path("pass2", base_dir=self.tmp_base))

        common_ids = {g["id"] for g in gold_rows if g["id"] in records1 and g["id"] in records2}
        common_gold = [g for g in gold_rows if g["id"] in common_ids]
        self.assertEqual(len(common_gold), 20)

        report1 = ev.score_against_gold(common_gold, records1)
        report2 = ev.score_against_gold(common_gold, records2)

        for field in ev.COMPARE_FIELDS:
            a1 = report1["per_field_accuracy"][field]["accuracy"]
            a2 = report2["per_field_accuracy"][field]["accuracy"]
            self.assertEqual(a1, a2, f"field {field} differs between identical files: {a1} vs {a2}")

        oa1 = report1["overall_accuracy"]["accuracy"]
        oa2 = report2["overall_accuracy"]["accuracy"]
        self.assertEqual(oa1, oa2)
        self.assertEqual(report1["overall_accuracy"]["correct"], report2["overall_accuracy"]["correct"])
        self.assertEqual(report1["overall_accuracy"]["total"], report2["overall_accuracy"]["total"])

        # No regressions possible when the two files are identical.
        for gold in common_gold:
            gid = gold["id"]
            for field in ev.ALL_MAIN_FIELDS:
                v1 = ev._predicted_value(records1[gid], field)
                v2 = ev._predicted_value(records2[gid], field)
                self.assertEqual(v1, v2)


if __name__ == "__main__":
    unittest.main()
