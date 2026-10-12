"""Tests for the numpy-only image tools, object fit/refine, shape detection, and font checks."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import detect_shapes  # noqa: E402
import font_check  # noqa: E402
import image_ops  # noqa: E402
import object_fit  # noqa: E402


WHITE = (255, 255, 255)


def canvas(w=1600, h=900):
    image = Image.new("RGB", (w, h), WHITE)
    return image, ImageDraw.Draw(image)


def text_block(draw, x, y, w, glyph_h, lines=1, pitch=None, color=(17, 17, 17)):
    """Fake text: one row of glyph-like blocks per line."""
    pitch = pitch or int(glyph_h * 1.5)
    for line in range(lines):
        top = y + line * pitch
        for gx in range(x, x + w - 8, 14):
            draw.rectangle([gx, top, gx + 9, top + glyph_h - 1], fill=color)


class ImageOpsTests(unittest.TestCase):
    def test_edge_f1(self):
        image, draw = canvas(200, 100)
        draw.rectangle([40, 30, 120, 70], outline=(0, 0, 0), width=2)
        a = np.array(image)
        self.assertAlmostEqual(image_ops.edge_f1(a, a), 1.0)
        shifted = np.roll(np.roll(a, 6, axis=1), 6, axis=0)
        self.assertLess(image_ops.edge_f1(a, shifted), 0.3)

    def test_label_components(self):
        mask = np.zeros((20, 30), dtype=bool)
        mask[2:5, 2:10] = True
        mask[10:18, 20:28] = True
        mask[4:12, 9] = True  # joins nothing else: touches first block only
        comps = sorted(image_ops.label_components(mask), key=lambda c: c["bbox"][0])
        self.assertEqual(len(comps), 2)
        self.assertEqual(comps[1]["bbox"], [20, 10, 8, 8])

    def test_ink_box_and_border_ink(self):
        image, draw = canvas(300, 200)
        draw.rectangle([0, 0, 299, 199], outline=(200, 0, 0), width=3)  # enclosing frame
        draw.rectangle([100, 80, 160, 100], fill=(0, 0, 0))
        a = np.array(image)
        self.assertEqual(image_ops.ink_box(a, [90, 70, 170, 110]), [100, 80, 161, 101])
        # A region that clips the frame: plain ink_box sees it, ignore_border_ink does not.
        self.assertEqual(image_ops.ink_box(a, [0, 60, 200, 120])[0], 0)
        self.assertEqual(image_ops.ink_box(a, [0, 60, 200, 120], ignore_border_ink=True), [100, 80, 161, 101])

    def test_ink_lines(self):
        image, draw = canvas(400, 200)
        text_block(draw, 50, 40, 200, 20, lines=3, pitch=32)
        lines = image_ops.ink_lines(np.array(image), [30, 20, 300, 160])
        self.assertEqual(len(lines), 3)
        self.assertEqual([b[1] for b in lines], [40, 72, 104])


class ObjectFitTests(unittest.TestCase):
    def test_box_refines_onto_source(self):
        src, d = canvas()
        d.rectangle([200, 200, 499, 399], fill=(31, 78, 121))
        ren, d = canvas()
        d.rectangle([215, 190, 499, 399], fill=(31, 78, 121))
        page = {"width_px": 1600, "height_px": 900, "objects": [{"id": "bar", "type": "rect", "bbox": [215, 190, 285, 210]}]}
        report = object_fit.measure_page(page["objects"], np.array(src), np.array(ren))
        self.assertEqual(report["objects"][0]["status"], "off")
        self.assertEqual(object_fit.refine_page(page, report["objects"]), ["bar"])
        self.assertEqual(page["objects"][0]["bbox"], [200.0, 200.0, 300.0, 200.0])

    def test_clipped_window_is_widened(self):
        # Render is smaller than the source: the source must not be clipped to the render's window.
        src, d = canvas()
        d.rectangle([200, 200, 539, 399], fill=(31, 78, 121))
        ren, d = canvas()
        d.rectangle([200, 200, 499, 399], fill=(31, 78, 121))
        obj = {"id": "bar", "type": "rect", "bbox": [200, 200, 300, 200]}
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 1600, 900)
        self.assertEqual(fit["status"], "off")
        self.assertEqual(fit["dx1"], 40)

    def test_text_font_size_then_position(self):
        src, d = canvas()
        text_block(d, 100, 100, 400, 20)
        ren, d = canvas()
        text_block(d, 110, 104, 400, 25)
        obj = {"id": "t", "type": "text", "bbox": [104, 96, 420, 40], "text": "x" * 28, "style": {"font_size_pt": 22}}
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 1600, 900)
        self.assertAlmostEqual(fit["scale_h"], 0.8)
        page = {"width_px": 1600, "height_px": 900, "objects": [obj]}
        object_fit.refine_page(page, [fit])
        # V5.4.1 caps one-round font changes at -13%; the uncapped 0.8
        # correction would be 17.5 pt, so the safe result is 19 pt.
        self.assertEqual(obj["style"]["font_size_pt"], 19.0)
        self.assertLess(obj["bbox"][0], 104)  # also moved left onto the source anchor

    def test_subpoint_font_change_takes_one_half_point(self):
        obj = {"id": "y-60", "type": "text", "bbox": [0, 0, 40, 20], "text": "60", "style": {"font_size_pt": 12.5}}
        # 6.7% is under a point, so the snap must not jump a whole point to 11.5.
        changed, _clamped = object_fit._scale_fonts(obj, 0.933)
        self.assertTrue(changed)
        self.assertEqual(obj["style"]["font_size_pt"], 12.0)

    def test_wrap_detected_for_single_line(self):
        src, d = canvas()
        text_block(d, 100, 100, 400, 20)
        ren, d = canvas()
        text_block(d, 100, 100, 200, 20, lines=2, pitch=30)
        obj = {"id": "t", "type": "text", "bbox": [96, 96, 260, 60], "text": "label", "style": {"font_size_pt": 20}}
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 1600, 900)
        self.assertEqual((fit["source_lines"], fit["render_lines"]), (1, 2))
        object_fit.refine_page({"width_px": 1600, "height_px": 900, "objects": [obj]}, [fit])
        self.assertFalse(obj["style"]["wrap"])
        self.assertGreater(obj["bbox"][2], 260)

    def test_substituted_font_skips_letter_spacing(self):
        src, d = canvas()
        text_block(d, 100, 100, 500, 20)
        ren, d = canvas()
        text_block(d, 100, 100, 400, 20)
        base = {"type": "text", "bbox": [96, 96, 520, 40], "text": "x" * 36, "style": {"font_size_pt": 20, "font_face": "Microsoft YaHei"}}
        fit = object_fit.measure_object(dict(base, id="t"), np.array(src), np.array(ren), 1600, 900)
        for substituted, expect_spacing in ((set(), True), ({"Microsoft YaHei"}, False)):
            obj = {**base, "id": "t", "style": dict(base["style"])}
            object_fit.refine_page({"width_px": 1600, "height_px": 900, "objects": [obj]}, [fit], substituted)
            self.assertEqual("char_spacing_pt" in obj["style"], expect_spacing)

    def test_line_refine_keeps_direction(self):
        src, d = canvas()
        d.line([760, 330, 1040, 330], fill=(240, 90, 40), width=4)
        ren, d = canvas()
        d.line([780, 345, 1060, 345], fill=(240, 90, 40), width=4)
        obj = {"id": "a", "type": "line", "points": [1060, 345, 780, 345]}
        page = {"width_px": 1600, "height_px": 900, "objects": [obj]}
        report = object_fit.measure_page(page["objects"], np.array(src), np.array(ren))
        object_fit.refine_page(page, report["objects"])
        x1, y1, x2, y2 = obj["points"]
        self.assertGreater(x1, x2)  # still points right-to-left
        self.assertAlmostEqual(x1, 1040, delta=2)
        self.assertAlmostEqual(y1, 330, delta=2)


class DetectShapesTests(unittest.TestCase):
    def detect(self, image, exclude=None):
        return detect_shapes.detect(np.array(image), exclude)

    def test_measures_panels_outlines_rules(self):
        image, d = canvas()
        d.rounded_rectangle([120, 220, 639, 519], radius=24, fill=(255, 244, 239), outline=(240, 90, 40), width=3)
        d.rectangle([800, 200, 1399, 319], fill=(31, 78, 121))
        d.ellipse([900, 500, 1059, 659], fill=(240, 90, 40))
        d.rectangle([1100, 650, 1399, 829], outline=(34, 34, 34), width=2)
        d.rectangle([70, 149, 1529, 151], fill=(240, 90, 40))
        objs: dict = {}
        for o in self.detect(image):
            objs.setdefault(o["type"], []).append(o)
        card = objs["round_rect"][0]
        self.assertEqual(card["style"]["fill"].upper(), "#FFF4EF")
        self.assertEqual(card["style"]["line"].upper(), "#F05A28")
        self.assertAlmostEqual(card["style"]["corner_radius_px"], 24, delta=4)
        rects = sorted(objs["rect"], key=lambda o: o["bbox"][0])
        self.assertEqual([round(v) for v in rects[0]["bbox"]], [800, 200, 600, 120])
        self.assertIsNone(rects[1]["style"]["fill"])
        self.assertEqual(len(objs["ellipse"]), 1)
        self.assertEqual(len(objs["line"]), 1)
        self.assertAlmostEqual(objs["line"][0]["points"][1], 150, delta=1)

    def test_text_strokes_are_not_rules(self):
        image, d = canvas()
        d.rectangle([100, 100, 300, 104], fill=(0, 0, 0))  # like the glyph "一"
        self.assertEqual(self.detect(image, exclude=[[90, 90, 400, 40]]), [])

    def test_table_grid_on_zebra_rows_survives(self):
        image, d = canvas()
        for row in range(4):
            top = 300 + row * 50
            if row % 2:
                d.rectangle([200, top, 1199, top + 49], fill=(242, 242, 242))
        for row in range(5):
            y = 300 + row * 50
            d.line([200, y, 1199, y], fill=(150, 150, 150), width=1)
        rules = [o for o in self.detect(image) if o["type"] == "line"]
        self.assertEqual(sorted(round(r["points"][1]) for r in rules), [300, 350, 400, 450, 500])


class FontCheckTests(unittest.TestCase):
    PAYLOAD = {
        "pages": [
            {
                "objects": [
                    {"type": "text", "text": "标题", "style": {"font_face": "Microsoft YaHei"}},
                    {"type": "text", "runs": [{"text": "a", "font_face": "Calibri"}], "style": {}},
                ]
            }
        ]
    }

    def test_fonts_and_cjk(self):
        fonts, has_cjk = font_check.page_ir_fonts(self.PAYLOAD)
        self.assertEqual(fonts, {"Microsoft YaHei", "Calibri", "Arial"})
        self.assertTrue(has_cjk)

    def test_report_flags_missing_cjk_and_metric_compatible(self):
        resolved = {"Microsoft YaHei": "DejaVu Sans", "Calibri": "Carlito", "Arial": "Arial"}
        with mock.patch.object(font_check, "cjk_font_families", return_value=[]), mock.patch.object(
            font_check, "resolve_font", side_effect=resolved.get
        ):
            report = font_check.font_report(self.PAYLOAD)
        self.assertTrue(report["problems"])
        subs = {s["requested"]: s for s in report["substitutions"]}
        self.assertTrue(subs["Calibri"]["metric_compatible"])
        self.assertFalse(subs["Microsoft YaHei"]["metric_compatible"])
        self.assertEqual(object_fit.substituted_fonts({"fonts": report}), {"Microsoft YaHei"})


if __name__ == "__main__":
    unittest.main()


class WeightAndLineTests(unittest.TestCase):
    def test_single_line_fragments_do_not_duplicate_or_stretch_glyphs(self):
        obj = {
            "id": "sub", "type": "text", "bbox": [52, 88, 800, 36],
            "text": "合规 · 办公",
            "style": {"font_size_pt": 15, "color": "#333333", "wrap": False},
        }
        title = {
            "id": "title", "type": "text", "bbox": [48, 10, 960, 80],
            "style": {"font_size_pt": 40, "color": "#1F2A44"},
        }
        head = {
            "id": "head", "type": "rect", "bbox": [24, 146, 300, 78],
            "style": {"fill": "#2E8B57"},
        }
        images = []
        for top in (93, 95):
            image, d = canvas(1672, 941)
            # A detached descender just below the title bbox. The old color
            # merger attaches it across a 1px gap to the later left glyph,
            # leaving a second, overlapping line from the earlier right glyph.
            d.rectangle([56, 91, 59, top - 1], fill=(51, 51, 51))
            d.rectangle([80, top, 95, top + 23], fill=(51, 51, 51))
            d.rectangle([56, top + 1, 65, top + 23], fill=(51, 51, 51))
            d.ellipse([120, top + 9, 123, top + 12], fill=(51, 51, 51))
            d.rectangle([145, top, 160, top + 23], fill=(51, 51, 51))
            d.rectangle([24, 146, 323, 223], fill=(46, 139, 87))
            images.append(np.array(image))
        lines = object_fit._measure_lines(obj, *images, 1672, 941, [title, obj, head])
        self.assertEqual(lines, ([[56, 93, 161, 117]], [[56, 95, 161, 119]]))
        fit = object_fit.measure_object(obj, *images, 1672, 941, objects=[title, obj, head])
        self.assertEqual((fit["source_lines"], fit["render_lines"]), (1, 1))
        self.assertEqual(fit["scale_h"], 1.0)
        self.assertTrue(fit["width_comparable"])
        self.assertIsNone(fit["pitch_ratio"])
        for image, expected in zip(images, (lines[0], lines[1])):
            self.assertEqual(image_ops.ink_lines(image, [30, 80, 900, 160], bg_region=[52, 88, 852, 124]), expected)

    def test_own_lines_require_vertical_intersection(self):
        obj = {"bbox": [50, 90, 200, 36], "text": "中文", "style": {"font_size_pt": 15, "wrap": False}}
        outside = [[50, 82, 100, 90], [50, 126, 100, 130]]
        self.assertEqual(object_fit._own_lines(outside, obj, 1672, 941), [])
        self.assertEqual(object_fit._own_lines(outside + [[50, 94, 100, 118]], obj, 1672, 941),
                         [[50, 94, 100, 118]])

    def outline_blocks(self, draw, x, y, w, glyph_h, stroke):
        for gx in range(x, x + w - 20, 34):
            draw.rectangle([gx, y, gx + 27, y + glyph_h - 1], outline=(17, 17, 17), width=stroke)

    def test_heavier_source_turns_on_bold(self):
        src, d = canvas()
        self.outline_blocks(d, 100, 100, 600, 30, 6)
        ren, d = canvas()
        self.outline_blocks(d, 100, 100, 600, 30, 3)
        obj = {"id": "t", "type": "text", "bbox": [96, 95, 620, 40], "text": "x" * 17, "style": {"font_size_pt": 30}}
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 1600, 900)
        self.assertGreater(fit["weight_ratio"], 1.3)
        self.assertEqual(fit["weight_fix"], "bold")
        object_fit.refine_page({"width_px": 1600, "height_px": 900, "objects": [obj]}, [fit])
        self.assertTrue(obj["style"]["bold"])
        # Already bold and still lighter than the source: no toggle, a font hint instead.
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 1600, 900)
        self.assertIsNone(fit["weight_fix"])
        self.assertIn("heavier", fit["font_hint"])

    def test_neighbour_fragments_do_not_stretch_a_line(self):
        image, d = canvas(800, 300)
        text_block(d, 50, 60, 500, 40)
        # Upper radicals of the next line's glyphs, just below the first line.
        for gx in range(50, 400, 30):
            d.rectangle([gx, 103, gx + 12, 110], fill=(17, 17, 17))
        lines = image_ops.ink_lines(np.array(image), [20, 40, 600, 112])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][3] - lines[0][1], 40)

    def test_white_text_on_colored_header(self):
        image, d = canvas(800, 300)
        d.rectangle([0, 80, 600, 200], fill=(46, 139, 87))
        text_block(d, 150, 120, 300, 36, color=(255, 255, 255))
        lines = image_ops.ink_lines(np.array(image), [100, 60, 520, 220], bg_region=[120, 110, 480, 166])
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0][1:4:2], [120, 156])


    def test_substituted_font_uses_only_extreme_weight_evidence(self):
        src, d = canvas()
        self.outline_blocks(d, 100, 100, 600, 30, 6)
        ren, d = canvas()
        self.outline_blocks(d, 100, 100, 600, 30, 3)
        obj = {
            "id": "t",
            "type": "text",
            "bbox": [96, 95, 620, 40],
            "text": "x" * 17,
            "style": {"font_size_pt": 30, "font_face": "PingFang SC"},
        }
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 1600, 900, substituted={"PingFang SC"})
        self.assertGreaterEqual(fit["weight_ratio"], 1.35)
        self.assertEqual(fit["weight_fix"], "bold")

    def test_glyphs_under_16px_skip_weight(self):
        src, draw = canvas()
        self.outline_blocks(draw, 100, 100, 600, 10, 4)
        ren, draw = canvas()
        self.outline_blocks(draw, 100, 100, 600, 10, 1)
        obj = {"id": "t", "type": "text", "bbox": [96, 96, 620, 20], "text": "x" * 17, "style": {"font_size_pt": 12}}
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 1600, 900)
        self.assertLess(fit["first_line_source"][3] - fit["first_line_source"][1], 16)
        self.assertIsNone(fit.get("weight_fix"))
        self.assertNotIn("weight_ratio", fit)


class GapClusterTests(unittest.TestCase):
    def glyphs(self, draw, x, y, n, gw=10, gh=20, gap=4):
        for i in range(n):
            gx = x + i * (gw + gap)
            draw.rectangle([gx, y, gx + gw - 1, y + gh - 1], fill=(17, 17, 17))
        return n * gw + (n - 1) * gap

    def test_middle_phrase_excludes_neighbours_1_5em_away(self):
        # 960px-wide page: 20pt is about 20px, so 1.5em = 30px and 0.9em = 18px.
        image, d = canvas(960, 540)
        self.glyphs(d, 120, 80, 11)
        self.glyphs(d, 300, 80, 11)
        self.glyphs(d, 480, 80, 11)
        obj = {
            "id": "mid",
            "type": "text",
            "bbox": [300, 50, 150, 80],
            "text": "中" * 11,
            "style": {"font_size_pt": 20},
        }
        fit = object_fit.measure_object(obj, np.array(image), np.array(image), 960, 540)
        self.assertLess(fit["source_width"], 180)
        self.assertGreater(fit["first_line_source"][0], 290)
        self.assertLess(fit["first_line_source"][2], 460)

    def test_text_mask_expands_other_text_by_0_3_em(self):
        obj = {"id": "a", "type": "text", "bbox": [100, 100, 80, 30], "style": {"font_size_pt": 20}}
        other = {"id": "b", "type": "text", "bbox": [300, 100, 40, 20], "style": {"font_size_pt": 20}}
        mask = object_fit._measurement_mask(obj, [obj, other], [0, 0, 960, 540], 960, 540)
        em = object_fit._em_px(other, 960, 540)
        self.assertTrue(mask[110, int(300 - 0.15 * em)])
        self.assertFalse(mask[110, int(300 - 0.8 * em)])

    def test_icon_base_above_box_is_not_the_first_line(self):
        obj = {"id": "t", "type": "text", "bbox": [100, 100, 80, 30], "style": {"font_size_pt": 20}}
        # Centre is 0.6em above the box: inside the old fallback, outside the 0.5em band.
        icon = [100, 80, 140, 88]
        text = [100, 104, 180, 124]
        kept = object_fit._lines_in_vertical_band([icon, text], obj, 960, 540)
        self.assertEqual(kept, [text])


class FontProfileTests(unittest.TestCase):
    RENDER = {"Microsoft YaHei", "微软雅黑", "STKaiti", "华文楷体", "DejaVu Sans", "Liberation Sans"}

    def test_infer_style(self):
        from pageir_common import infer_style

        self.assertEqual(
            [infer_style(n) for n in ("STKaiti", "Microsoft YaHei", "STFangsong", "Songti SC", "LXGW WenKai", "Arial")],
            ["kai", "sans", "fangsong", "song", "kai", None],
        )

    def test_choose_prefers_exact_fonts_of_the_same_style(self):
        from pageir_common import choose_font

        installed = {"Microsoft YaHei", "STKaiti"}
        self.assertEqual(choose_font("sans", "mac", installed, None, self.RENDER), ("Microsoft YaHei", True))
        self.assertEqual(choose_font("kai", "mac", installed, None, self.RENDER), ("STKaiti", True))
        # Without installed fonts the mac default stays, rendered with a substitute.
        self.assertEqual(choose_font("sans", "mac", set(), None, self.RENDER), ("PingFang SC", False))
        # No exact 宋体 here: keep the style, never fall back to another style's exact font.
        self.assertEqual(choose_font("song", "mac", installed, None, self.RENDER), ("Songti SC", False))

    def test_preference_wins(self):
        from pageir_common import choose_font

        self.assertEqual(choose_font("kai", "mac", set(), {"kai": "LXGW WenKai"}, self.RENDER), ("LXGW WenKai", False))

    def test_font_plan_lists_exact_matches(self):
        with mock.patch.object(font_check, "render_families", return_value=self.RENDER):
            plan = font_check.font_plan("mac", {"Microsoft YaHei", "STKaiti"})
        self.assertEqual(plan["exact_match_fonts"], ["Microsoft YaHei", "STKaiti"])
        self.assertEqual(plan["recommended"]["sans"], {"font": "Microsoft YaHei", "exact_render": True})
        self.assertEqual(plan["recommended"]["kai"], {"font": "STKaiti", "exact_render": True})
        self.assertFalse(plan["recommended"]["latin_sans"]["exact_render"])


class BatchToolTests(unittest.TestCase):
    def test_uncovered_ink_finds_left_out_content(self):
        import tempfile

        import delivery_gate

        image, d = canvas()
        d.rectangle([100, 100, 400, 200], fill=(31, 78, 121))
        d.rectangle([900, 500, 1100, 600], fill=(240, 90, 40))
        page = {"width_px": 1600, "height_px": 900, "objects": [{"id": "a", "type": "rect", "bbox": [100, 100, 301, 101]}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.png"
            image.save(path)
            share, regions = delivery_gate.uncovered_ink(path, page)
            self.assertGreater(share, 0.3)
            self.assertTrue(any(abs(r[0] - 900) <= 8 and abs(r[1] - 500) <= 8 for r in regions))
            page["objects"].append({"id": "b", "type": "rect", "bbox": [900, 500, 201, 101]})
            self.assertEqual(delivery_gate.uncovered_ink(path, page)[0], 0.0)

    def test_gate_fails_slide_without_fidelity_loop(self):
        import tempfile

        import delivery_gate

        with tempfile.TemporaryDirectory() as tmp:
            row = delivery_gate.check_slide(Path(tmp), 0.9)
        self.assertEqual(row["level"], "FAIL")
        self.assertIn("fidelity_loop.py was not run", row["issues"][0])

    def test_assemble_renames_pages_and_rejects_other_ratios(self):
        import json as _json
        import tempfile

        import assemble_deck

        def write(folder, w, h, platform="mac", fonts=()):
            folder.mkdir()
            path = folder / "page_ir.best.json"
            path.write_text(_json.dumps({
                "schema_version": "1.1",
                "target": {"platform": platform, "installed_fonts": list(fonts)},
                "pages": [{"id": "slide-1", "width_px": w, "height_px": h, "objects": [
                    {"id": "img", "type": "image", "bbox": [0, 0, 10, 10], "path": "assets/a.png"}]}],
            }), encoding="utf-8")
            return path

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            a = write(tmp / "s1", 1600, 900, fonts=["STKaiti"])
            b = write(tmp / "s2", 1920, 1080, fonts=["Microsoft YaHei"])
            deck = assemble_deck.assemble([a, b])
            self.assertEqual([p["id"] for p in deck["pages"]], ["slide-01", "slide-02"])
            self.assertEqual(deck["target"]["installed_fonts"], ["Microsoft YaHei", "STKaiti"])
            self.assertEqual(deck["pages"][1]["objects"][0]["path"], str((tmp / "s2" / "assets" / "a.png").resolve()))
            c = write(tmp / "s3", 900, 1600)
            with self.assertRaises(ValueError):
                assemble_deck.assemble([a, c])


class InfographicSegmentationTests(unittest.TestCase):
    """Cards with 1 px light borders and line icons, a bar chart on axes (an AI-generated promo slide)."""

    def build(self):
        image, d = canvas(1672, 941)
        for x in (60, 457, 852, 1249):
            d.rounded_rectangle([x, 233, x + 364, 527], radius=12, outline=(222, 222, 222), width=1, fill=(255, 255, 255))
            # A line icon: frame, inner glyph-like bars, and a stand.
            d.rounded_rectangle([x + 120, 265, x + 240, 345], radius=6, outline=(240, 90, 40), width=5)
            d.rectangle([x + 150, 290, x + 210, 300], fill=(240, 90, 40))
            d.rectangle([x + 150, 312, x + 190, 322], fill=(240, 90, 40))
            d.rectangle([x + 170, 355, x + 190, 372], fill=(240, 90, 40))
        d.line([117, 622, 117, 809], fill=(170, 170, 170), width=2)
        d.line([117, 809, 906, 809], fill=(170, 170, 170), width=2)
        d.rectangle([178, 747, 304, 808], fill=(204, 204, 206))
        d.rectangle([706, 640, 837, 808], fill=(240, 90, 40))
        return image

    def test_icons_become_assets_and_structure_becomes_shapes(self):
        import extract_assets

        image = np.array(self.build())
        page = {"width_px": 1672, "height_px": 941, "objects": []}
        assets = extract_assets.find_assets(image, page)
        self.assertEqual(len(assets), 4)
        for (x, y, w, h), card_x in zip(sorted(assets), (60, 457, 852, 1249)):
            self.assertAlmostEqual(x, card_x + 118, delta=4)  # the whole icon, frame included
            self.assertGreater(h, 100)  # frame + stand in one asset
        page["objects"] = [{"id": f"a{i}", "type": "image", "bbox": b} for i, b in enumerate(assets)]
        shapes = detect_shapes.detect(image, [o["bbox"] for o in page["objects"]])
        cards = [o for o in shapes if o["type"] == "round_rect"]
        self.assertEqual(len(cards), 4)
        for card in cards:
            self.assertAlmostEqual(card["bbox"][2], 365, delta=4)
            self.assertEqual(card["style"]["line"][:3], "#DE")
        bars = sorted((o for o in shapes if o["type"] == "rect"), key=lambda o: o["bbox"][0])
        self.assertEqual([round(b["bbox"][0]) for b in bars], [178, 706])
        lines = [o for o in shapes if o["type"] == "line"]
        self.assertEqual(len(lines), 2)  # y axis, and the x axis joined across the bars
        x_axis = max(lines, key=lambda o: abs(o["points"][2] - o["points"][0]))
        self.assertLess(x_axis["points"][0], 125)
        self.assertGreater(x_axis["points"][2], 895)
