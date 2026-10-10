from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "validate_page_ir.py"
spec = importlib.util.spec_from_file_location("validate_page_ir", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class PageIRValidationTests(unittest.TestCase):
    def test_minimal_fixture_is_valid(self):
        path = ROOT / "examples" / "minimal" / "page_ir.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(module.validate_page_ir(payload, path.parent), [])

    def test_duplicate_object_id_is_rejected(self):
        payload = {
            "schema_version": "1.0",
            "pages": [
                {
                    "id": "slide-1",
                    "width_px": 100,
                    "height_px": 100,
                    "background": "#FFFFFF",
                    "objects": [
                        {"id": "x", "type": "rect", "bbox": [0, 0, 10, 10]},
                        {"id": "x", "type": "rect", "bbox": [20, 20, 10, 10]},
                    ],
                }
            ],
        }
        errors = module.validate_page_ir(payload, ROOT)
        self.assertTrue(any("duplicate object id" in item for item in errors))

    def test_out_of_bounds_is_rejected(self):
        payload = {
            "schema_version": "1.0",
            "pages": [
                {
                    "id": "slide-1",
                    "width_px": 100,
                    "height_px": 100,
                    "background": "#FFFFFF",
                    "objects": [{"id": "x", "type": "rect", "bbox": [90, 90, 20, 20]}],
                }
            ],
        }
        errors = module.validate_page_ir(payload, ROOT)
        self.assertTrue(any("outside the page" in item for item in errors))

    def test_features_fixture_is_valid(self):
        path = ROOT / "examples" / "features" / "page_ir.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(module.validate_page_ir(payload, path.parent), [])

    def test_short_hex_color_is_accepted(self):
        payload = page([{"id": "x", "type": "rect", "bbox": [0, 0, 10, 10], "style": {"fill": "#FFF", "line": None}}])
        self.assertEqual(module.validate_page_ir(payload, ROOT), [])

    def test_bad_style_color_is_rejected(self):
        payload = page([{"id": "t", "type": "text", "bbox": [0, 0, 50, 10], "text": "a", "style": {"color": "red"}}])
        errors = module.validate_page_ir(payload, ROOT)
        self.assertTrue(any("style.color" in item for item in errors))

    def test_zero_length_line_is_rejected(self):
        payload = page([{"id": "l", "type": "line", "points": [5, 5, 5, 5]}])
        errors = module.validate_page_ir(payload, ROOT)
        self.assertTrue(any("zero-length" in item for item in errors))

    def test_reversed_line_is_valid(self):
        payload = page([{"id": "l", "type": "line", "points": [90, 80, 10, 20], "style": {"end_arrow": "triangle"}}])
        self.assertEqual(module.validate_page_ir(payload, ROOT), [])

    def test_runs_are_validated(self):
        good = page([{"id": "t", "type": "text", "bbox": [0, 0, 50, 10], "runs": [{"text": "a", "bold": True}]}])
        self.assertEqual(module.validate_page_ir(good, ROOT), [])
        bad = page([{"id": "t", "type": "text", "bbox": [0, 0, 50, 10], "runs": [{"text": "a", "color": "#12"}]}])
        self.assertTrue(any("runs[0]" in item for item in module.validate_page_ir(bad, ROOT)))

    def test_corner_radius_only_on_round_rect(self):
        payload = page([{"id": "r", "type": "rect", "bbox": [0, 0, 10, 10], "style": {"corner_radius_px": 4}}])
        errors = module.validate_page_ir(payload, ROOT)
        self.assertTrue(any("corner_radius_px" in item for item in errors))

    def test_non_positive_font_size_is_rejected(self):
        payload = page([{"id": "t", "type": "text", "bbox": [0, 0, 50, 10], "text": "a", "style": {"font_size_pt": 0}}])
        errors = module.validate_page_ir(payload, ROOT)
        self.assertTrue(any("font_size_pt" in item for item in errors))

    def test_unknown_schema_version_is_rejected(self):
        payload = page([])
        payload["schema_version"] = "2.0"
        errors = module.validate_page_ir(payload, ROOT)
        self.assertTrue(any("schema_version" in item for item in errors))


def page(objects):
    return {
        "schema_version": "1.1",
        "pages": [{"id": "slide-1", "width_px": 100, "height_px": 100, "background": "#FFFFFF", "objects": objects}],
    }


if __name__ == "__main__":
    unittest.main()


class FontAndLayoutLintTests(unittest.TestCase):
    def text(self, oid, bbox, font="PingFang SC", text="项"):
        return {"id": oid, "type": "text", "bbox": bbox, "text": text, "style": {"font_face": font}}

    def lint(self, objects, target=None):
        payload = page(objects)
        if target is not None:
            payload["target"] = target
        return module.validate_page_ir_detailed(payload, ROOT)

    def test_render_sandbox_font_is_an_error(self):
        errors, _ = self.lint([self.text("t", [0, 0, 50, 10], "Noto Sans CJK SC")], {"platform": "mac"})
        self.assertTrue(any("render-sandbox font" in e and "PingFang SC" in e for e in errors))

    def test_installed_font_is_allowed(self):
        errors, warnings = self.lint(
            [self.text("t", [0, 0, 50, 10], "Noto Sans CJK SC")],
            {"platform": "mac", "installed_fonts": ["Noto Sans CJK SC"]},
        )
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_windows_font_on_mac_warns(self):
        errors, warnings = self.lint([self.text("t", [0, 0, 50, 10], "Microsoft YaHei")], {"platform": "mac"})
        self.assertEqual(errors, [])
        self.assertTrue(any("not a standard mac font" in w for w in warnings))

    def test_bad_platform(self):
        errors, _ = self.lint([], {"platform": "linux"})
        self.assertTrue(any("target.platform" in e for e in errors))

    def test_number_and_unit_in_separate_boxes_warns(self):
        _, warnings = self.lint([self.text("num", [10, 40, 50, 30], text="128"), self.text("unit", [62, 48, 15, 22])])
        self.assertTrue(any("'num' and 'unit' sit on one line" in w for w in warnings))

    def test_separate_lines_do_not_warn(self):
        _, warnings = self.lint([self.text("a", [10, 10, 60, 20], text="a"), self.text("b", [10, 40, 60, 20], text="b")])
        self.assertFalse(any("one line" in w for w in warnings))


class FontPreferenceTests(unittest.TestCase):
    def test_preferences_are_validated(self):
        payload = page([])
        payload["target"] = {"platform": "mac", "installed_fonts": ["STKaiti"], "font_preferences": {"kai": "STKaiti"}}
        self.assertEqual(module.validate_page_ir(payload, ROOT), [])
        payload["target"]["font_preferences"] = {"handwriting": "STKaiti"}
        self.assertTrue(any("font_preferences" in e for e in module.validate_page_ir(payload, ROOT)))
