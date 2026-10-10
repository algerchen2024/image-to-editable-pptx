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

    def test_mac_target_restores_size_and_weight(self):
        """WorkBuddy-style slide for a Mac: runs for brand+title and number+unit, white header text."""
        sys.path.insert(0, str(SCRIPTS))
        from render_compare import render_slides

        truth = {
            "schema_version": "1.1",
            "target": {"platform": "mac", "installed_fonts": []},
            "default_font_face": "Helvetica Neue",
            "pages": [{"id": "s", "width_px": 1672, "height_px": 941, "background": "#FFFFFF", "objects": [
                {"id": "title", "type": "text", "bbox": [48, 10, 960, 80], "z": 10,
                 "runs": [{"text": "WorkBuddy ", "font_face": "Helvetica Neue"}, {"text": "平安内网准入筛选逻辑"}],
                 "style": {"font_face": "PingFang SC", "font_size_pt": 40, "bold": True, "color": "#1F2A44",
                           "valign": "mid", "wrap": False}},
                {"id": "total", "type": "text", "bbox": [54, 365, 300, 95], "z": 10,
                 "runs": [{"text": "1,000", "font_face": "Helvetica Neue", "font_size_pt": 58, "color": "#1E7B45"},
                          {"text": "项", "font_size_pt": 22, "color": "#1E7B45"}],
                 "style": {"font_face": "PingFang SC", "font_size_pt": 22, "bold": True, "valign": "bottom", "wrap": False}},
                {"id": "sub", "type": "text", "bbox": [52, 88, 800, 36], "z": 10,
                 "text": "严选合规安全的生产力能力 · 只为更高效、更安全的内网办公",
                 "style": {"font_face": "PingFang SC", "font_size_pt": 15, "color": "#333333", "wrap": False}},
                {"id": "head", "type": "rect", "bbox": [24, 146, 300, 78], "z": 1, "style": {"fill": "#2E8B57", "line": None}},
                {"id": "headtxt", "type": "text", "bbox": [60, 158, 230, 54], "z": 10, "text": "全量基线池",
                 "style": {"font_face": "PingFang SC", "font_size_pt": 24, "bold": True, "color": "#FFFFFF",
                           "align": "center", "valign": "mid", "wrap": False}},
            ]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "truth.json").write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")
            subprocess.run(["node", str(SCRIPTS / "compile_page_ir.js"), str(tmp / "truth.json"), str(tmp / "truth.pptx")], check=True, capture_output=True)
            rendered = render_slides(tmp / "truth.pptx", tmp, 130)[0]
            Image.open(rendered).convert("RGB").resize((1672, 941), Image.Resampling.LANCZOS).save(tmp / "source.png")

            perturbed = json.loads(json.dumps(truth))
            objs = {o["id"]: o for o in perturbed["pages"][0]["objects"]}
            objs["headtxt"]["style"].update(bold=False, font_size_pt=21)
            objs["sub"]["style"]["font_size_pt"] = 13
            objs["sub"]["bbox"][1] += 6
            objs["title"]["style"]["font_size_pt"] = 36
            (tmp / "perturbed.json").write_text(json.dumps(perturbed, ensure_ascii=False), encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "fidelity_loop.py"), str(tmp / "perturbed.json"), str(tmp / "source.png"),
                 "--out", str(tmp / "out.pptx"), "--workdir", str(tmp / "q")],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            best = json.loads((tmp / "q" / "page_ir.best.json").read_text(encoding="utf-8"))
            got = {o["id"]: o["style"] for o in best["pages"][0]["objects"]}
            self.assertTrue(got["headtxt"]["bold"])
            self.assertAlmostEqual(got["headtxt"]["font_size_pt"], 24, delta=0.5)
            self.assertAlmostEqual(got["sub"]["font_size_pt"], 15, delta=0.5)
            self.assertAlmostEqual(got["title"]["font_size_pt"], 40, delta=1)

            gate = subprocess.run(
                [sys.executable, str(SCRIPTS / "delivery_gate.py"), str(tmp / "q"), "--json"], capture_output=True, text=True
            )
            self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
            self.assertEqual(json.loads(gate.stdout)[0]["level"], "HIGH")


if __name__ == "__main__":
    unittest.main()
