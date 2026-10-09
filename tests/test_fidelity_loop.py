"""End-to-end: a perturbed PageIR converges back to its source render. Needs LibreOffice + Poppler + Node."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def tools_ready() -> bool:
    if not all(shutil.which(t) for t in ("node", "pdftoppm")) or not (shutil.which("soffice") or shutil.which("libreoffice")):
        return False
    return subprocess.run(["node", "-e", "require('pptxgenjs')"], cwd=ROOT, capture_output=True).returncode == 0


@unittest.skipUnless(tools_ready(), "needs node+pptxgenjs, LibreOffice and pdftoppm")
class FidelityLoopTests(unittest.TestCase):
    def test_perturbed_fixture_converges(self):
        sys.path.insert(0, str(SCRIPTS))
        from render_compare import render_slides

        truth = json.loads((ROOT / "examples" / "features" / "page_ir.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
            subprocess.run(["node", str(SCRIPTS / "compile_page_ir.js"), str(tmp / "truth.json"), str(tmp / "truth.pptx")], check=True, capture_output=True)
            rendered = render_slides(tmp / "truth.pptx", tmp, 120)[0]
            Image.open(rendered).convert("RGB").resize((1600, 900), Image.Resampling.LANCZOS).save(tmp / "source.png")

            perturbed = json.loads(json.dumps(truth))
            objs = {o["id"]: o for o in perturbed["pages"][0]["objects"]}
            objs["card"]["bbox"] = [140, 235, 480, 280]
            objs["title"]["style"]["font_size_pt"] = 26
            objs["title"]["bbox"][1] += 10
            objs["arrow-left"]["points"] = [1060, 345, 780, 345]
            (tmp / "perturbed.json").write_text(json.dumps(perturbed), encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "fidelity_loop.py"), str(tmp / "perturbed.json"), str(tmp / "source.png"),
                 "--out", str(tmp / "out.pptx"), "--workdir", str(tmp / "q")],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            summary = json.loads(proc.stdout)
            self.assertGreater(summary["rounds"][-1]["object_fit_ratio"] or 0, summary["rounds"][0]["object_fit_ratio"])
            best = json.loads((tmp / "q" / "page_ir.best.json").read_text(encoding="utf-8"))
            got = {o["id"]: o for o in best["pages"][0]["objects"]}
            self.assertEqual(got["title"]["style"]["font_size_pt"], 30)
            for actual, expected in zip(got["card"]["bbox"], [120, 220, 520, 300]):
                self.assertAlmostEqual(actual, expected, delta=3)
            for actual, expected in zip(got["arrow-left"]["points"], [1040, 330, 760, 330]):
                self.assertAlmostEqual(actual, expected, delta=3)


if __name__ == "__main__":
    unittest.main()
