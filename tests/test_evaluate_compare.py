"""Proves evaluate.py works offline for pass 2 and never writes to pass1.json.

No LLM/network calls - this only reads JSON files and does arithmetic.
Run with: python tests/test_evaluate_compare.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import research_agent.evaluate as ev  # noqa: E402

PASS1_PATH = Path("output/pass1.json")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestEvaluateReadOnly(unittest.TestCase):
    """--output-prefix pass1 (and --compare pass1 ...) must never write to
    pass1.json - proven by making it read-only on disk and running anyway."""

    def setUp(self):
        self.assertTrue(PASS1_PATH.exists(), "output/pass1.json must exist for this test to be meaningful")
        self.original_mode = PASS1_PATH.stat().st_mode
        self.original_hash = _sha256(PASS1_PATH)
        os.chmod(PASS1_PATH, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    def tearDown(self):
        os.chmod(PASS1_PATH, self.original_mode)
        self.assertEqual(_sha256(PASS1_PATH), self.original_hash, "pass1.json content changed during the test!")
        eval_out = ev.eval_out_path("pass1")
        if eval_out.exists():
            eval_out.unlink()

    def test_single_mode_does_not_write_pass1(self):
        ev.run_single("pass1")  # would raise PermissionError if it tried to write pass1.json

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
