from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import validate_skill  # noqa: E402


class DisplayVersionTests(unittest.TestCase):
    def check(self, version: str, display: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
            return validate_skill.check_display_version(Path(tmp), display)

    def test_repository_is_consistent(self):
        self.assertEqual(validate_skill.validate(ROOT), [])

    def test_matching_major_passes(self):
        self.assertEqual(self.check("5.0.2", "GPT Image to Editable PPTX V5"), [])

    def test_minor_version_must_be_shown(self):
        self.assertEqual(self.check("5.1.0", "GPT Image to Editable PPTX V5.1"), [])
        self.assertTrue(self.check("5.1.0", "GPT Image to Editable PPTX V5"))
        self.assertTrue(self.check("5.2.0", "GPT Image to Editable PPTX V5.1"))

    def test_mismatched_major_fails(self):
        self.assertTrue(self.check("6.0.0", "GPT Image to Editable PPTX V5"))

    def test_missing_suffix_fails(self):
        self.assertTrue(self.check("5.0.0", "GPT Image to Editable PPTX"))


if __name__ == "__main__":
    unittest.main()
