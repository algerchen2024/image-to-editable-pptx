from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
import sys

sys.path.insert(0, str(SCRIPTS))

from compile_page_ir import compile_payload
from pageir_common import choose_font, font_issue, latin_font_for
from render_fonts import (
    closest_drawable,
    load_font_cache,
    match_probed_families,
    scan_font_dirs,
    select_drawable,
    substitute_payload,
    substitutions_for_payload,
    _fc_list,
    _cache_key,
)
from runtime_check import build_report
from validate_page_ir import validate_page_ir_detailed


class V541FontTests(unittest.TestCase):
    def test_font_selection_order(self):
        # 1. User preference wins
        self.assertEqual(
            choose_font("kai", "mac", {"Kaiti SC"}, {"kai": "CustomKai"}, {"customkai"})[0],
            "CustomKai",
        )
        # 2. Detected target font that render env can draw -> exact
        self.assertEqual(
            choose_font("sans", "mac", {"SimHei"}, None, {"simhei"})[0],
            "SimHei",
        )
        # 3. OS-bundled default for platform (mac: PingFang SC, windows: Microsoft YaHei)
        self.assertEqual(choose_font("sans", "mac", set(), None, set())[0], "PingFang SC")
        self.assertEqual(choose_font("sans", "windows", set(), None, set())[0], "Microsoft YaHei")
        # 4. Unknown platform -> neutral "any" (+mn-ea / +mn-lt)
        self.assertEqual(choose_font("sans", "unknown", set(), None, set())[0], "+mn-ea")
        self.assertEqual(choose_font("latin_sans", "unknown", set(), None, set())[0], "+mn-lt")

    def test_any_mode_validation_and_errors(self):
        self.assertEqual(choose_font("sans", "any")[0], "+mn-ea")
        self.assertEqual(choose_font("latin_sans", "any")[0], "+mn-lt")
        self.assertIsNone(font_issue("+mn-ea", "any", set()))
        self.assertIsNone(font_issue("+mj-ea", "any", set()))
        self.assertIsNone(font_issue("+mn-lt", "any", set()))
        self.assertIsNone(font_issue("Arial", "any", set()))
        # Concrete CJK font in any mode is an error unless installed
        issue = font_issue("Microsoft YaHei", "any", set())
        self.assertIsNotNone(issue)
        self.assertEqual(issue[0], "error")
        self.assertIn("+mn-ea", issue[1])
        # If listed in installed_fonts, it is accepted
        self.assertIsNone(font_issue("Microsoft YaHei", "any", {"Microsoft YaHei"}))

    def test_latin_font_resolution(self):
        # Explicit latin_font_face
        self.assertEqual(latin_font_for({"latin_font_face": "Arial"}, "mac", "PingFang SC"), "Arial")
        # Backwards compatible: cjk_face is already Latin
        self.assertEqual(latin_font_for({}, "mac", "Arial"), "Arial")
        # Fallback to platform default Latin font
        self.assertEqual(latin_font_for({}, "mac", "PingFang SC"), "Helvetica Neue")
        self.assertEqual(latin_font_for({}, "windows", "Microsoft YaHei"), "Arial")
        self.assertEqual(latin_font_for({}, "any", "+mn-ea"), "+mn-lt")
        self.assertEqual(latin_font_for({}, "any", "+mj-ea"), "+mj-lt")

    def test_a_ea_and_latin_are_split_and_theme_is_neutral(self):
        payload = {
            "schema_version": "1.1",
            "target": {"platform": "any", "installed_fonts": []},
            "pages": [{
                "id": "s",
                "width_px": 1600,
                "height_px": 900,
                "objects": [{
                    "id": "t",
                    "type": "text",
                    "bbox": [0, 0, 500, 80],
                    "runs": [{"text": "中文 ABC", "font_face": "+mn-ea", "latin_font_face": "+mn-lt"}],
                    "style": {"font_face": "+mn-ea", "latin_font_face": "+mn-lt"},
                }],
            }],
        }
        errors, _ = validate_page_ir_detailed(payload, ROOT)
        self.assertEqual(errors, [])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.pptx"
            compile_payload(payload, ROOT, out)
            with zipfile.ZipFile(out) as deck:
                slide = deck.read("ppt/slides/slide1.xml").decode("utf-8")
                theme = deck.read("ppt/theme/theme1.xml").decode("utf-8")
                # Slide checks
                self.assertIn('a:ea typeface="+mn-ea"', slide)
                self.assertIn('a:latin typeface="+mn-lt"', slide)
                self.assertIn('a:cs typeface="+mn-lt"', slide)
                self.assertIn('lang="zh-CN"', slide)
                self.assertIn('altLang="en-US"', slide)
                # Theme checks
                self.assertIn('a:ea typeface=""', theme)
                self.assertNotIn('script="Hans"', theme)
                self.assertNotIn('script="Hant"', theme)
                self.assertNotIn('script="Jpan"', theme)
                self.assertNotIn('script="Hang"', theme)

                # Ensure no concrete CJK font name exists anywhere in any xml file
                cjk_names = ["PingFang", "Microsoft YaHei", "SimSun", "KaiTi", "Songti", "SimHei", "新細明體", "宋体"]
                for name in deck.namelist():
                    if name.endswith(".xml"):
                        xml_str = deck.read(name).decode("utf-8")
                        for cjk in cjk_names:
                            self.assertNotIn(cjk, xml_str, f"Found {cjk} in {name}")

    def test_arrow_head_attributes(self):
        # Valid arrow head attributes
        payload = {
            "schema_version": "1.1",
            "target": {"platform": "any"},
            "pages": [{
                "id": "s",
                "width_px": 1000,
                "height_px": 500,
                "objects": [
                    {
                        "id": "l1",
                        "type": "line",
                        "points": [10, 10, 200, 10],
                        "style": {"color": "#000", "width_pt": 2, "end_arrow": "triangle", "head_w": "lg", "head_len": "sm"},
                    },
                    {
                        "id": "l2",
                        "type": "line",
                        "points": [10, 30, 200, 30],
                        "style": {"color": "#000", "width_pt": 2, "end_arrow": "triangle", "head_w": "invalid"},
                    },
                ],
            }],
        }
        errors, _ = validate_page_ir_detailed(payload, ROOT)
        self.assertTrue(any("head_w must be one of sm, med, lg" in e for e in errors))

        # Compile valid arrow line
        valid_payload = {
            "schema_version": "1.1",
            "target": {"platform": "any"},
            "pages": [{
                "id": "s",
                "width_px": 1000,
                "height_px": 500,
                "objects": [{
                    "id": "l1",
                    "type": "line",
                    "points": [10, 10, 200, 10],
                    "style": {"color": "#000", "width_pt": 2, "end_arrow": "triangle", "head_w": "lg", "head_len": "med"},
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "arrow.pptx"
            compile_payload(valid_payload, ROOT, out)
            with zipfile.ZipFile(out) as deck:
                slide = deck.read("ppt/slides/slide1.xml").decode("utf-8")
                self.assertIn('w="lg"', slide)
                self.assertIn('len="med"', slide)

    def test_head_size_px_is_two_positive_numbers(self):
        def line(style):
            return {
                "schema_version": "1.1",
                "pages": [{
                    "id": "s",
                    "width_px": 400,
                    "height_px": 200,
                    "objects": [{
                        "id": "a",
                        "type": "line",
                        "points": [20, 40, 180, 40],
                        "style": style,
                    }],
                }],
            }
        bad, _ = validate_page_ir_detailed(line({"color": "#000", "width_pt": 2, "head_size_px": [0, 10]}), ROOT)
        self.assertTrue(any("head_size_px must be two positive numbers" in e for e in bad))
        good, _ = validate_page_ir_detailed(
            line({"color": "#000", "width_pt": 2, "end_arrow": "triangle", "head_size_px": [24, 18]}), ROOT
        )
        self.assertFalse(any("head_size_px" in e for e in good))

    def test_head_size_compiles_to_native_triangle(self):
        payload = {
            "schema_version": "1.1",
            "pages": [{
                "id": "s",
                "width_px": 1000,
                "height_px": 500,
                "objects": [{
                    "id": "a",
                    "type": "line",
                    "points": [100, 80, 400, 80],
                    "style": {"color": "#E10600", "width_pt": 3, "end_arrow": "triangle", "head_size_px": [30, 27]},
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "arrow.pptx"
            compile_payload(payload, ROOT, out)
            with zipfile.ZipFile(out) as deck:
                slide = deck.read("ppt/slides/slide1.xml").decode("utf-8")
        self.assertNotIn("tailEnd", slide)
        self.assertNotIn("headEnd", slide)
        self.assertIn('prst="triangle"', slide)
        self.assertIn("E10600", slide)
        self.assertIn('rot="5400000"', slide)

    def test_render_copy_replaces_theme_and_delivery_payload(self):
        payload = {
            "schema_version": "1.1",
            "target": {"platform": "any"},
            "pages": [{
                "id": "s",
                "width_px": 100,
                "height_px": 100,
                "objects": [{
                    "id": "t",
                    "type": "text",
                    "bbox": [0, 0, 90, 30],
                    "text": "中文",
                    "style": {"font_face": "+mn-ea", "latin_font_face": "+mn-lt"},
                }],
            }],
        }
        copy_payload, substitutions = substitute_payload(payload)
        # Original payload remains unchanged
        self.assertEqual(payload["pages"][0]["objects"][0]["style"]["font_face"], "+mn-ea")
        # Substituted payload replaced with drawable font
        self.assertTrue(any(item["requested"] == "+mn-ea" and item["substituted"] for item in substitutions))
        self.assertNotEqual(copy_payload["pages"][0]["objects"][0]["style"]["font_face"], "+mn-ea")
        # Metric compatibility is False for theme references
        for sub in substitutions:
            if sub["requested"] in {"+mn-ea", "+mn-lt"}:
                self.assertFalse(sub["metric_compatible"])

    def test_closest_drawable_uses_style_candidates(self):
        self.assertEqual(closest_drawable("missing", "latin_sans", {"Arial"}), "Arial")
        self.assertEqual(closest_drawable("+mn-ea", "sans", {"Noto Sans CJK SC"}), "Noto Sans CJK SC")

    def test_scan_font_dirs_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            families = scan_font_dirs((Path(tmp),))
            self.assertEqual(families, set())

    def test_fc_list_branch_uses_fontconfig_families(self):
        fake = mock.Mock(returncode=0, stdout="Noto Sans CJK SC,Arial\nLiberation Sans\n")
        with mock.patch("render_fonts.shutil.which", return_value="/usr/bin/fc-list"), mock.patch(
            "render_fonts.subprocess.run", return_value=fake
        ) as run:
            self.assertEqual(_fc_list(), {"Noto Sans CJK SC", "Arial", "Liberation Sans"})
            run.assert_called_once()

    def test_runtime_check_structure(self):
        report = build_report("chi_sim+eng", "any")
        self.assertIn("ready_ocr", report)
        self.assertIn("render_drawable", report["fonts"])
        self.assertIn("target_detected", report["fonts"])
        self.assertIn("exact_match_fonts", report["fonts"])
        self.assertIn("recommended", report["fonts"])
        self.assertIn("hints", report)
        # Ensure no suggestions to install fonts in notes
        for note in report.get("notes", []):
            self.assertNotIn("install a CJK font", note)

    def test_pdffonts_prefix_match_and_system_dirs_are_not_drawable(self):
        sample = """name                                 type              encoding         emb sub uni object ID
------------------------------------ ----------------- ---------------- --- --- --- ---------
BAAAAA+NotoSansCJKsc-Regular         CID TrueType      Identity-H       yes yes yes     12  0
AAAAAA+LiberationSans                TrueType          WinAnsi          yes yes yes     13  0
LinuxLibertineG                      Type 1            Custom           yes yes yes     14  0
Carlito                              TrueType          WinAnsi          yes yes yes     15  0
"""
        families = {"Noto Sans CJK SC", "Liberation Sans", "Helvetica Neue", "PingFang SC", "Times New Roman"}
        matched = match_probed_families(families, sample)
        self.assertEqual(matched, {"Noto Sans CJK SC", "Liberation Sans"})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundled, user, system = root / "lo", root / "user", root / "sys"
            for directory in (bundled, user, system):
                directory.mkdir()
            (bundled / "LiberationSans.ttf").write_bytes(b"not-a-font")
            (user / "NotoSansCJKsc.otf").write_bytes(b"not-a-font")
            (system / "HelveticaNeue.ttf").write_bytes(b"not-a-font")

            def fake_names(font_path: Path) -> set[str]:
                return {font_path.stem}

            with mock.patch("render_fonts._pillow_families", side_effect=fake_names):
                scanned = {
                    "bundled": scan_font_dirs((bundled,)),
                    "user": scan_font_dirs((user,)),
                    "system": scan_font_dirs((system,)),
                }
            self.assertEqual(scanned["system"], {"HelveticaNeue"})
            failed = select_drawable(
                platform="darwin",
                fontconfig=None,
                probed=None,
                bundled=scanned["bundled"],
                user=scanned["user"],
                system=scanned["system"],
            )
            self.assertEqual(failed, {"LiberationSans", "NotoSansCJKsc"})
            confirmed = select_drawable(
                platform="darwin",
                fontconfig=None,
                probed=matched,
                bundled=scanned["bundled"],
                user=scanned["user"],
                system={"Helvetica Neue", "PingFang SC"},
            )
            self.assertEqual(confirmed, {"Noto Sans CJK SC", "Liberation Sans"})
            self.assertEqual(
                select_drawable(
                    platform="linux",
                    fontconfig={"DejaVu Sans"},
                    probed=None,
                    bundled={"Liberation Sans"},
                    user=set(),
                    system={"Helvetica Neue"},
                ),
                {"DejaVu Sans"},
            )

    def test_old_cache_format_is_ignored_and_key_includes_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            path.write_text(json.dumps({"families": ["Helvetica Neue"]}), encoding="utf-8")
            self.assertIsNone(load_font_cache(path))
            path.write_text(json.dumps({"version": 2, "families": ["Liberation Sans"]}), encoding="utf-8")
            self.assertEqual(load_font_cache(path)["families"], ["Liberation Sans"])
        self.assertNotEqual(_cache_key("/soffice", [], {"Arial"}), _cache_key("/soffice", [], {"Arial", "Helvetica"}))

    def test_helvetica_maps_to_liberation_but_is_not_metric_compatible(self):
        families = {"Liberation Sans", "Liberation Serif", "Noto Sans CJK SC"}
        self.assertEqual(closest_drawable("Helvetica Neue", "latin_sans", families), "Liberation Sans")
        self.assertEqual(closest_drawable("Helvetica", "latin_sans", families), "Liberation Sans")
        self.assertEqual(closest_drawable("Arial", "latin_sans", families), "Liberation Sans")
        self.assertEqual(closest_drawable("Times New Roman", "latin_serif", families), "Liberation Serif")
        # A face the probe actually confirmed is kept.
        self.assertEqual(closest_drawable("Arial", "latin_sans", families | {"Arial"}), "Arial")
        payload = {
            "target": {"platform": "mac"},
            "pages": [{
                "objects": [
                    {"type": "text", "text": "A", "style": {"font_face": "Helvetica Neue"}},
                    {"type": "text", "text": "B", "style": {"font_face": "Arial"}},
                ]
            }],
        }
        with mock.patch("render_fonts.drawable_families", return_value=set(families)):
            subs = {item["requested"]: item for item in substitutions_for_payload(payload)}
        self.assertEqual(subs["Helvetica Neue"]["rendered_with"], "Liberation Sans")
        self.assertTrue(subs["Helvetica Neue"]["substituted"])
        self.assertFalse(subs["Helvetica Neue"]["metric_compatible"])
        self.assertEqual(subs["Arial"]["rendered_with"], "Liberation Sans")
        self.assertTrue(subs["Arial"]["substituted"])
        self.assertTrue(subs["Arial"]["metric_compatible"])




if __name__ == "__main__":
    unittest.main()
