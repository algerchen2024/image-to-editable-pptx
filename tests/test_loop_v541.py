"""Tests for V5.4.1 fidelity loop robustness, icon extraction, arrows, and CJK spacing."""

from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import detect_shapes  # noqa: E402
import extract_assets  # noqa: E402
import image_ops  # noqa: E402
import object_fit  # noqa: E402
import ocr_to_pageir  # noqa: E402


WHITE = (255, 255, 255)
BLACK = (17, 17, 17)


def create_canvas(w: int = 800, h: int = 600) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (w, h), WHITE)
    return img, ImageDraw.Draw(img)


class MaskingAndGuardTests(unittest.TestCase):
    def test_measurement_mask_preserves_own_bbox(self):
        obj = {"id": "t1", "type": "text", "bbox": [100, 100, 200, 40]}
        other_text = {"id": "t2", "type": "text", "bbox": [150, 80, 100, 40]}
        card = {
            "id": "c1",
            "type": "round_rect",
            "bbox": [80, 80, 300, 100],
            "style": {"fill": "#F0F0F0", "line": "#000000", "line_width_pt": 2.0},
        }
        region = [50, 50, 400, 200]
        mask = object_fit._measurement_mask(obj, [obj, other_text, card], region, 800, 600)
        # Own glyphs stay visible: below and beside the neighbour.
        self.assertFalse(mask[125:140, 100:300].any())
        self.assertFalse(mask[100:120, 100:140].any())
        # The neighbour's own box stays masked where the two boxes overlap.
        self.assertTrue(mask[105:115, 160:240].any())
        # Other text outside own bbox must be masked
        self.assertTrue(mask[80:100, 150:250].any())
        # Card border must be masked
        self.assertTrue(mask[78:84, 80:380].any())

    def test_plausibility_guard_flags_suspect_measurement(self):
        src, d_src = create_canvas()
        # Draw unusually tall ink (exceeding 1.6x em)
        d_src.rectangle([100, 100, 300, 160], fill=BLACK)
        ren, d_ren = create_canvas()
        d_ren.rectangle([100, 100, 300, 120], fill=BLACK)
        obj = {"id": "t1", "type": "text", "bbox": [90, 90, 320, 80], "style": {"font_size_pt": 18}}
        res = object_fit.measure_object(obj, np.array(src), np.array(ren), 800, 600)
        self.assertEqual(res["status"], "off")
        self.assertEqual(res.get("suspect"), "measurement")

        # refine_page should not change font size or move position when suspect: measurement
        page = {"width_px": 800, "height_px": 600, "objects": [obj]}
        changed = object_fit.refine_page(page, [res])
        self.assertEqual(changed, [])
        self.assertEqual(obj["style"]["font_size_pt"], 18)
        self.assertEqual(obj["bbox"], [90, 90, 320, 80])

    def test_per_round_font_scale_cap_and_clamped(self):
        obj = {"id": "t", "type": "text", "bbox": [100, 100, 200, 30], "style": {"font_size_pt": 20.0}}
        # Needed scale_h = 1.30, but cap is 1.15
        fit = {
            "id": "t",
            "status": "off",
            "scale_h": 1.30,
            "source_width": 200,
            "render_width": 200,
            "source_lines": 1,
            "render_lines": 1,
            "anchor_dx": 0.0,
            "anchor_dy": 0.0,
        }
        page = {"width_px": 800, "height_px": 600, "objects": [obj]}
        changed = object_fit.refine_page(page, [fit], baseline_state={"t": 20.0})
        self.assertEqual(changed, ["t"])
        self.assertTrue(fit.get("clamped"))
        # 20 * 1.15 = 23.0 pt
        self.assertEqual(obj["style"]["font_size_pt"], 23.0)

    def test_cumulative_font_scale_cap_stops_change_and_flags_suspect(self):
        # Baseline is 20.0 pt. Current font size is already 31.0 pt (within 1.6x = 32).
        obj = {"id": "t", "type": "text", "bbox": [100, 100, 200, 30], "style": {"font_size_pt": 31.0}}
        # Request another 1.10 scale -> would be 34.1 pt (> 20 * 1.6 = 32.0 pt)
        fit = {
            "id": "t",
            "status": "off",
            "scale_h": 1.10,
            "source_width": 200,
            "render_width": 200,
            "source_lines": 1,
            "render_lines": 1,
            "anchor_dx": 0.0,
            "anchor_dy": 0.0,
        }
        page = {"width_px": 800, "height_px": 600, "objects": [obj]}
        changed = object_fit.refine_page(page, [fit], baseline_state={"t": 20.0})
        self.assertEqual(changed, [])
        self.assertEqual(fit.get("suspect"), "cumulative_font_cap")
        self.assertEqual(obj["style"]["font_size_pt"], 31.0)

        # The other side of the same [0.5, 1.6] window.
        small = {"id": "t", "type": "text", "bbox": [100, 100, 200, 30], "style": {"font_size_pt": 11.0}}
        shrink = dict(fit, scale_h=0.87, suspect=None)
        changed = object_fit.refine_page({"width_px": 800, "height_px": 600, "objects": [small]}, [shrink], baseline_state={"t": 20.0})
        self.assertEqual(changed, [])
        self.assertEqual(shrink.get("suspect"), "cumulative_font_cap")
        self.assertEqual(small["style"]["font_size_pt"], 11.0)

    def test_letter_spacing_cap(self):
        obj = {"id": "t", "type": "text", "bbox": [100, 100, 200, 30], "text": "ABCD", "style": {"font_size_pt": 20.0}}
        # Very large width disparity
        fit = {
            "id": "t",
            "status": "off",
            "scale_h": 1.0,
            "width_comparable": True,
            "source_width": 350.0,
            "render_width": 200.0,
            "source_lines": 1,
            "render_lines": 1,
            "anchor_dx": 0.0,
            "anchor_dy": 0.0,
        }
        page = {"width_px": 800, "height_px": 600, "objects": [obj]}
        object_fit.refine_page(page, [fit])
        # |char_spacing_pt| must be capped to 0.12 * font_size = 2.4 pt
        self.assertAlmostEqual(obj["style"]["char_spacing_pt"], 2.4, places=2)

    def test_frozen_object_is_not_refined(self):
        obj = {"id": "t", "type": "text", "bbox": [100, 100, 200, 30], "style": {"font_size_pt": 18.0}}
        fit = {
            "id": "t",
            "status": "off",
            "scale_h": 1.10,
            "source_width": 200,
            "render_width": 200,
            "source_lines": 1,
            "render_lines": 1,
            "anchor_dx": 0.0,
            "anchor_dy": 0.0,
        }
        page = {"id": "p1", "width_px": 800, "height_px": 600, "objects": [obj]}
        changed = object_fit.refine_page(page, [fit], frozen={"p1:t"})
        self.assertEqual(changed, [])
        self.assertEqual(obj["style"]["font_size_pt"], 18.0)

    def test_rollback_strikes_twice_before_freeze(self):
        import fidelity_loop

        page = {
            "id": "slide-1",
            "width_px": 800,
            "height_px": 600,
            "objects": [
                {"id": "card2-title", "type": "text", "bbox": [100, 100, 200, 40], "style": {"font_size_pt": 24.0}}
            ],
        }
        payload = {"pages": [page]}
        old_obj = {"id": "card2-title", "type": "text", "bbox": [100, 100, 200, 40], "style": {"font_size_pt": 18.0}}
        before_fit = {"id": "card2-title", "status": "off", "scale_h": 1.0, "anchor_dx": 2.0, "anchor_dy": 0.0}
        now_fit = {"id": "card2-title", "status": "off", "scale_h": 1.0, "anchor_dx": 5.0, "anchor_dy": 0.0}
        slide = {"slide": 1, "object_fit": {"objects": [now_fit]}}
        pending = {"slide-1:card2-title": {"object": old_obj, "fit": before_fit}}
        strikes: dict[str, int] = {}
        frozen: set[str] = set()

        rolled, newly_frozen, limits = fidelity_loop.apply_rollbacks(payload, [slide], pending, strikes, frozen)
        self.assertEqual(page["objects"][0]["style"]["font_size_pt"], 18.0)
        self.assertEqual(rolled, ["slide-1:card2-title"])
        self.assertEqual(newly_frozen, [])
        self.assertNotIn("slide-1:card2-title", frozen)
        self.assertEqual(strikes["slide-1:card2-title"], 1)
        self.assertEqual(limits["slide-1:card2-title"], {"position_only": True})
        self.assertNotIn("strike", page["objects"][0])

        # The following refine may move the box but must not touch the font size,
        # even when the measured glyph height asks for a larger size.
        page["objects"][0]["style"]["font_size_pt"] = 18.0
        move_fit = {**now_fit, "scale_h": 1.4, "anchor_dx": 8.0}
        before_box = list(page["objects"][0]["bbox"])
        changed = object_fit.refine_page(page, [move_fit], limits=limits)
        self.assertEqual(page["objects"][0]["style"]["font_size_pt"], 18.0)
        self.assertNotEqual(page["objects"][0]["bbox"], before_box)
        self.assertNotEqual(changed, [])

        # Second worsening freezes the object.
        page["objects"][0]["style"]["font_size_pt"] = 24.0
        pending = {"slide-1:card2-title": {"object": old_obj, "fit": before_fit}}
        rolled, newly_frozen, limits = fidelity_loop.apply_rollbacks(payload, [slide], pending, strikes, frozen)
        self.assertEqual(strikes["slide-1:card2-title"], 2)
        self.assertEqual(newly_frozen, ["slide-1:card2-title"])
        self.assertIn("slide-1:card2-title", frozen)
        self.assertEqual(limits, {})
        changed = object_fit.refine_page(page, [now_fit], frozen=frozen)
        self.assertEqual(changed, [])
        self.assertEqual(page["objects"][0]["style"]["font_size_pt"], 18.0)

    def test_line_stroke_stays_masked_inside_the_text_box(self):
        text = {"id": "t", "type": "text", "bbox": [100, 100, 80, 40], "style": {"font_size_pt": 16}}
        line = {"id": "l", "type": "line", "points": [90, 120, 200, 120], "style": {"color": "#111111", "width_pt": 2}}
        mask = object_fit._measurement_mask(text, [text, line], [50, 50, 250, 200], 400, 300)
        self.assertTrue(mask[120, 140])
        self.assertFalse(mask[108, 140])

    def test_round_rect_stroke_stays_masked_inside_the_text_box(self):
        # The outline crosses the text box. Clearing the box must not bring it back,
        # including the rounded corner, which sits inside a rectangular border band.
        text = {
            "id": "badge-high",
            "type": "text",
            "bbox": [45, 45, 150, 80],
            "text": "HIGH",
            "style": {"font_size_pt": 18, "color": "#F35817"},
        }
        shape = {
            "id": "badge-shape",
            "type": "round_rect",
            "bbox": [40, 40, 180, 100],
            "style": {"fill": None, "line": "#F45818", "line_width_pt": 3.0, "corner_radius_px": 36},
        }
        mask = object_fit._measurement_mask(text, [text, shape], [0, 0, 400, 300], 800, 600)
        self.assertTrue(mask[42, 130])  # top stroke, inside the text box
        self.assertTrue(mask[55, 50])  # corner arc, inside the text box and inside the radius
        self.assertFalse(mask[90, 130])  # interior, where the glyphs are

    def test_same_color_outline_is_not_a_second_text_line(self):
        img, draw = create_canvas(800, 600)
        orange = (243, 88, 23)
        draw.rounded_rectangle([40, 40, 220, 140], radius=36, outline=orange, width=4)
        draw.rectangle([80, 82, 180, 102], fill=orange)
        text = {
            "id": "badge-high",
            "type": "text",
            "bbox": [60, 55, 140, 70],
            "text": "HIGH",
            "style": {"font_size_pt": 18, "color": "#F35817", "bold": True, "align": "center"},
        }
        shape = {
            "id": "badge-shape",
            "type": "round_rect",
            "bbox": [40, 40, 180, 100],
            "style": {"fill": None, "line": "#F35817", "line_width_pt": 3.6, "corner_radius_px": 36},
        }
        arr = np.array(img)
        fit = object_fit.measure_object(text, arr, arr, 800, 600, objects=[text, shape])
        self.assertEqual(fit["status"], "ok")
        self.assertEqual((fit["source_lines"], fit["render_lines"]), (1, 1))
        self.assertGreater(fit["first_line_source"][1], 60)

    def test_tick_measurement_excludes_the_axis(self):
        img, draw = create_canvas(300, 300)
        gray = (176, 178, 181)
        draw.line([120, 40, 120, 260], fill=gray, width=3)
        draw.line([100, 150, 111, 150], fill=gray, width=3)
        tick = {"id": "tick", "type": "line", "points": [100, 150, 111, 150], "style": {"color": "#B0B2B5", "width_pt": 1.15}}
        axis = {"id": "axis", "type": "line", "points": [120, 40, 120, 260], "style": {"color": "#B0B2B5", "width_pt": 1.72}}
        arr = np.array(img)
        fit = object_fit.measure_object(tick, arr, arr, 300, 300, objects=[tick, axis])
        self.assertEqual(fit["status"], "ok")
        self.assertGreaterEqual(fit["source_ink"][0], 96)
        self.assertLessEqual(fit["source_ink"][2], 118)

    def test_bar_ignores_the_value_label_and_axis(self):
        img, draw = create_canvas(400, 300)
        draw.rectangle([120, 160, 300, 250], fill=(204, 204, 206))
        draw.rectangle([200, 130, 240, 155], fill=(37, 37, 43))  # the "38" above the bar
        draw.line([116, 40, 116, 260], fill=(176, 178, 181), width=3)
        bar = {"id": "shape-6", "type": "rect", "bbox": [120, 140, 180, 110], "style": {"fill": "#CCCCCE"}}
        label = {"id": "v-38", "type": "text", "bbox": [198, 128, 44, 30], "text": "38", "style": {"font_size_pt": 14, "color": "#25252B"}}
        axis = {"id": "rule", "type": "line", "points": [116, 40, 116, 260], "style": {"color": "#B0B2B5", "width_pt": 1.7}}
        arr = np.array(img)
        fit = object_fit.measure_object(bar, arr, arr, 400, 300, objects=[bar, label, axis])
        self.assertEqual(fit["status"], "ok")
        self.assertGreater(fit["source_ink"][1], 150)
        self.assertLess(fit["source_ink"][0], 130)

    def test_phantom_tick_is_removed(self):
        page = {
            "id": "slide-1",
            "width_px": 400,
            "height_px": 300,
            "objects": [
                {"id": "tick-1", "type": "line", "points": [100, 150, 111, 150], "style": {"color": "#B0B2B5", "width_pt": 1.15}},
                {"id": "keep", "type": "line", "points": [20, 10, 20, 200], "style": {"color": "#111111", "width_pt": 1}},
            ],
        }
        fits = [
            {"id": "tick-1", "type": "line", "status": "extra_in_render"},
            {"id": "keep", "type": "line", "status": "no_ink"},
        ]
        changed = object_fit.refine_page(page, fits)
        self.assertEqual(changed, ["tick-1"])
        self.assertEqual([o["id"] for o in page["objects"]], ["keep"])

    def test_fill_that_dominates_the_window_is_still_measured(self):
        img, draw = create_canvas(500, 220)
        draw.rounded_rectangle([30, 40, 230, 170], radius=28, fill=(253, 239, 229))
        draw.rounded_rectangle([270, 40, 470, 170], radius=28, fill=(253, 239, 229))
        draw.rectangle([90, 90, 170, 120], fill=(243, 71, 8))
        color = np.array([253, 239, 229])
        box = object_fit.color_box(np.array(img), [0, 0, 500, 220], color, [30, 40, 230, 170])
        self.assertIsNotNone(box)
        self.assertLess(abs(box[0] - 30), 10)
        self.assertLess(box[2], 250)
        self.assertGreater(box[2], 200)

    def test_shifted_source_text_is_recovered_by_color(self):
        src, draw = create_canvas(400, 200)
        draw.rectangle([136, 80, 186, 100], fill=(53, 56, 61))
        ren, draw = create_canvas(400, 200)
        draw.rectangle([40, 80, 90, 100], fill=(53, 56, 61))
        obj = {
            "id": "y-20",
            "type": "text",
            "bbox": [30, 70, 70, 30],
            "text": "20",
            "style": {"font_size_pt": 14, "color": "#35383D"},
        }
        fit = object_fit.measure_object(obj, np.array(src), np.array(ren), 400, 200)
        self.assertEqual(fit["status"], "off")
        self.assertGreater(fit["anchor_dx"], 20)



class AssetExtractionCardTests(unittest.TestCase):
    def test_extract_icon_inside_card_in_pageir(self):
        img, d = create_canvas(800, 600)
        # Draw a bordered card
        d.rounded_rectangle([100, 100, 400, 400], radius=10, outline=(200, 200, 200), width=2, fill=(255, 255, 255))
        # Inside the card, draw a line icon (e.g. a small square with cross)
        d.rectangle([220, 220, 280, 280], outline=(240, 90, 40), width=4)
        d.line([220, 220, 280, 280], fill=(240, 90, 40), width=4)

        card_obj = {
            "id": "card-1",
            "type": "round_rect",
            "bbox": [100, 100, 300, 300],
            "style": {"fill": "#FFFFFF", "line": "#C8C8C8", "line_width_pt": 1.5},
        }
        page = {"width_px": 800, "height_px": 600, "objects": [card_obj]}
        assets = extract_assets.find_assets(np.array(img), page)
        self.assertGreaterEqual(len(assets), 1)
        ax, ay, aw, ah = assets[0]
        self.assertAlmostEqual(ax + aw / 2, 250, delta=10)
        self.assertAlmostEqual(ay + ah / 2, 250, delta=10)


class ArrowTests(unittest.TestCase):
    def test_arrow_head_classification(self):
        self.assertEqual(detect_shapes.classify_arrow_head(2.5), "sm")
        self.assertEqual(detect_shapes.classify_arrow_head(4.0), "med")
        self.assertEqual(detect_shapes.classify_arrow_head(6.0), "lg")

    def test_preset_lg_is_actionable_when_mismatched(self):
        # Preset lg is no waiver: a mismatch between source and render head size remains actionable.
        self.assertTrue(object_fit._head_class_mismatch_is_actionable({"head_w": "lg", "end_arrow": "triangle"}, "lg", "med"))
        self.assertTrue(object_fit._head_class_mismatch_is_actionable({"end_arrow": "triangle"}, "lg", "med"))
        self.assertFalse(object_fit._head_class_mismatch_is_actionable({"head_w": "lg"}, "lg", "lg"))

    def test_thick_open_arrow_with_small_render_is_off(self):
        # Synthetic test required by G1: thick-stroked large open arrow must be off when rendered with a small head.
        src, d_src = create_canvas(400, 300)
        d_src.line([50, 150, 200, 150], fill=(244, 85, 24), width=4)
        d_src.line([184, 136, 200, 150], fill=(244, 85, 24), width=4)
        d_src.line([184, 164, 200, 150], fill=(244, 85, 24), width=4)

        ren, d_ren = create_canvas(400, 300)
        d_ren.line([50, 150, 194, 150], fill=(244, 85, 24), width=2)
        d_ren.polygon([(200, 150), (192, 146), (192, 154)], fill=(244, 85, 24))

        obj = {
            "id": "arrow-test",
            "type": "line",
            "points": [50, 150, 200, 150],
            "style": {
                "color": "#F45518",
                "width_pt": 1.5,
                "end_arrow": "triangle",
                "head_w": "sm",
                "head_len": "sm",
            },
        }
        res = object_fit.measure_object(obj, np.array(src), np.array(ren), 400, 300)
        self.assertEqual(res["status"], "off")
        self.assertGreater(abs(res["dy0"]), 3)
        self.assertGreater(abs(res["dy1"]), 3)

    def test_arrow_head_ignores_a_nearby_same_color_icon(self):
        img, draw = create_canvas(400, 220)
        orange = (244, 85, 24)
        draw.line([40, 120, 180, 120], fill=orange, width=4)
        draw.polygon([(180, 120), (164, 112), (164, 128)], fill=orange)
        # Disconnected icon inside the search radius, wider than the real head.
        draw.rectangle([150, 40, 210, 90], fill=orange)
        alone, _ = create_canvas(400, 220)
        d2 = ImageDraw.Draw(alone)
        d2.line([40, 120, 180, 120], fill=orange, width=4)
        d2.polygon([(180, 120), (164, 112), (164, 128)], fill=orange)
        with_icon = detect_shapes.measure_arrow_head(np.array(img), [40, 120, 180, 120], "#F45518", 4)
        plain = detect_shapes.measure_arrow_head(np.array(alone), [40, 120, 180, 120], "#F45518", 4)
        self.assertIsNotNone(plain)
        self.assertIsNotNone(with_icon)
        self.assertEqual(with_icon["size"], plain["size"])
        self.assertLess(with_icon["head_w_px"], 30)

    def test_half_step_after_shape_rollback(self):
        obj = {"id": "pill", "type": "rect", "bbox": [100.0, 100.0, 80.0, 40.0], "style": {"fill": "#F05A28"}}
        fit = {"id": "pill", "status": "off", "dx0": 20.0, "dy0": 0.0, "dx1": 20.0, "dy1": 0.0}
        object_fit.refine_box(obj, fit, 800, 600, step_scale=0.5)
        self.assertEqual(obj["bbox"][0], 110.0)
        self.assertNotIn("step_scale", obj)

    def test_refine_line_grows_head_size_at_most_30_percent(self):
        obj = {
            "id": "arrow",
            "type": "line",
            "points": [100.0, 100.0, 300.0, 100.0],
            "style": {"end_arrow": "triangle", "color": "#111111", "width_pt": 2},
        }
        fit = {
            "id": "arrow",
            "status": "off",
            "dx0": 0.0,
            "dy0": 0.0,
            "dx1": 0.0,
            "dy1": 0.0,
            "source_ink": [100, 80, 300, 120],
            "render_ink": [100, 95, 300, 105],
        }
        self.assertTrue(object_fit.refine_line(obj, fit, 800, 600))
        width, length = obj["style"]["head_size_px"]
        # Render thickness is 10px; one round may grow it by 30%, length = 0.9 * width.
        self.assertEqual(width, 13.0)
        self.assertEqual(length, 11.7)
        self.assertTrue(object_fit.refine_line(obj, fit, 800, 600))
        width, length = obj["style"]["head_size_px"]
        self.assertEqual(width, 16.9)
        self.assertEqual(length, 15.2)

    def test_refine_line_steps_arrow_head_size(self):
        obj = {
            "id": "arrow",
            "type": "line",
            "points": [100.0, 100.0, 300.0, 100.0],
            "style": {"head_w": "sm", "head_len": "sm", "end_arrow": "triangle"},
        }
        fit = {
            "id": "arrow",
            "status": "off",
            "dx0": 0.0,
            "dy0": 0.0,
            "dx1": 0.0,
            "dy1": 0.0,
            "head_w_source": "lg",
            "head_w_render": "sm",
            "head_len_source": "lg",
            "head_len_render": "sm",
        }
        # Round 1: sm -> med
        changed = object_fit.refine_line(obj, fit, 800, 600)
        self.assertTrue(changed)
        self.assertEqual(obj["style"]["head_w"], "med")

        # Round 2: med -> lg
        fit["head_w_render"] = "med"
        fit["head_len_render"] = "med"
        changed = object_fit.refine_line(obj, fit, 800, 600)
        self.assertTrue(changed)
        self.assertEqual(obj["style"]["head_w"], "lg")

    def test_wide_arrow_head_uses_head_size_px(self):
        img, d = create_canvas(800, 600)
        d.line([100, 300, 400, 300], fill=(200, 0, 0), width=2)
        # Head much wider than 6 stroke widths. Direction is known, so it stays a line.
        d.polygon([(400, 300), (360, 260), (360, 340)], fill=(200, 0, 0))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            objects = detect_shapes.detect(np.array(img), assets_dir=Path(tmp) / "assets")
        headed = [o for o in objects if o.get("type") == "line" and (o.get("style") or {}).get("head_size_px")]
        self.assertGreaterEqual(len(headed), 1)
        width, length = headed[0]["style"]["head_size_px"]
        self.assertGreater(width, 12)
        self.assertGreater(length, 0)
        self.assertNotIn("head_w", headed[0]["style"])
        self.assertEqual([o for o in objects if o.get("type") == "image"], [])

    def test_unknown_arrow_direction_still_crops(self):
        decision = detect_shapes.rule_arrow_decision(
            {"points": [10, 10, 12, 10], "arrow": {"ratio": 8, "head_w_px": 40, "head_len_px": 30, "size": "lg"}}
        )
        self.assertEqual(decision["mode"], "image")
        kept = detect_shapes.rule_arrow_decision(
            {"points": [10, 10, 200, 10], "arrow": {"ratio": 8, "head_w_px": 40, "head_len_px": 30, "size": "lg"}}
        )
        self.assertEqual(kept["mode"], "head_size")
        self.assertEqual(kept["head_size_px"], [40.0, 30.0])


class CJKLatinSpacingTests(unittest.TestCase):
    def test_spacing_measurement_waits_for_size_alignment(self):
        obj = {
            "id": "t",
            "type": "text",
            "bbox": [40, 40, 260, 40],
            "text": "中 A",
            "style": {"font_size_pt": 18},
        }
        blank = np.full((160, 400, 3), 255, dtype=np.uint8)
        lines = ([[40, 50, 120, 70]], [[40, 50, 120, 70]])
        with mock.patch.object(object_fit, "_measure_lines", return_value=lines), mock.patch.object(
            object_fit, "_needs_spacing_removal", return_value=[(1, 2)]
        ) as needs:
            fit = {}
            object_fit.measure_text(obj, blank, blank, 400, 160, fit)
        self.assertEqual(fit["scale_h"], 1.0)
        needs.assert_called_once()

        with mock.patch.object(object_fit, "_measure_lines", return_value=([[40, 50, 120, 70]], [[40, 50, 120, 60]])), mock.patch.object(
            object_fit, "_needs_spacing_removal", return_value=[(1, 2)]
        ) as needs:
            fit = {}
            object_fit.measure_text(obj, blank, blank, 400, 160, fit)
        self.assertEqual(fit["scale_h"], 2.0)
        needs.assert_not_called()
        self.assertNotIn("spacing_remove", fit)

    def test_spacing_uses_source_and_render_ems(self):
        text = "中 A"
        with mock.patch.object(object_fit, "_boundary_gap", side_effect=[6.0, 6.0]):
            spans = object_fit._needs_spacing_removal(
                text, None, None, None, None, None, source_em=20.0, render_em=10.0
            )
        # Equal pixel gaps are materially different after em normalization:
        # 0.30em in the source versus 0.60em in the render.
        self.assertEqual(spans, [(1, 2)])

    def test_separator_spaces_are_kept_when_source_gap_is_real(self):
        for text in ("中 · 文", "中 / 文"):
            with mock.patch.object(object_fit, "_boundary_gap", side_effect=[5.0, 10.0, 5.0, 10.0]):
                spans = object_fit._needs_spacing_removal(
                    text, None, None, None, None, None, source_em=16.0, render_em=16.0
                )
            # Both boundaries touch the separator; 5px is 0.3125em, so
            # neither intentional separator space is removed.
            self.assertEqual(spans, [])

    def test_size_correction_precedes_spacing_removal(self):
        obj = {
            "id": "t",
            "type": "text",
            "bbox": [100, 100, 200, 30],
            "text": "中 A",
            "style": {"font_size_pt": 13.0},
        }
        fit = {
            "id": "t",
            "status": "off",
            "scale_h": 1.14,
            "spacing_remove": True,
            "spacing_spans": [(1, 2)],
            "source_width": 180,
            "render_width": 200,
            "source_lines": 1,
            "render_lines": 1,
            "anchor_dx": 0.0,
            "anchor_dy": 0.0,
        }
        object_fit.refine_page({"width_px": 800, "height_px": 600, "objects": [obj]}, [fit])
        self.assertEqual(obj["style"]["font_size_pt"], 15.0)
        self.assertEqual(obj["text"], "中 A")
        self.assertNotIn("spacing_removed", fit)

    def test_ocr_to_pageir_gap_threshold(self):
        tokens = ["中文", "AI", "模型"]
        # Small gap (5 px < 0.33 * 20 = 6.6 px)
        boxes_small = [[100, 10, 40, 20], [145, 10, 30, 20], [180, 10, 40, 20]]
        res_small = ocr_to_pageir.join_tokens_measured(tokens, boxes_small, em_px=20.0)
        self.assertEqual(res_small, "中文AI模型")

        # Large gap (10 px >= 0.33 * 20 = 6.6 px)
        boxes_large = [[100, 10, 40, 20], [150, 10, 30, 20], [190, 10, 40, 20]]
        res_large = ocr_to_pageir.join_tokens_measured(tokens, boxes_large, em_px=20.0)
        self.assertEqual(res_large, "中文 AI 模型")

    def test_refine_removes_spaces_before_letter_spacing(self):
        obj = {
            "id": "t",
            "type": "text",
            "bbox": [100, 100, 200, 30],
            "text": "测试 AI 模型",
            "style": {"font_size_pt": 18.0},
        }
        fit = {
            "id": "t",
            "status": "off",
            "scale_h": 1.0,
            "width_comparable": True,
            "source_width": 180.0,
            "render_width": 200.0,  # render is wider
            "source_lines": 1,
            "render_lines": 1,
            "anchor_dx": 0.0,
            "anchor_dy": 0.0,
        }
        page = {"width_px": 800, "height_px": 600, "objects": [obj]}
        # Round 1: removes spaces, does not add char_spacing_pt
        changed = object_fit.refine_page(page, [fit])
        self.assertEqual(changed, ["t"])
        self.assertEqual(obj["text"], "测试AI模型")
        self.assertNotIn("char_spacing_pt", obj["style"])
        self.assertTrue(fit.get("spacing_removed"))

    def test_substituted_render_gap_marks_cjk_latin_space(self):
        source, source_draw = create_canvas(400, 160)
        render, render_draw = create_canvas(400, 160)
        for x0, x1 in ((80, 100), (103, 123), (126, 146), (149, 169)):
            source_draw.rectangle([x0, 60, x1, 88], fill=BLACK)
        for x0, x1 in ((80, 100), (116, 136), (152, 172), (188, 208)):
            render_draw.rectangle([x0, 60, x1, 88], fill=BLACK)
        obj = {
            "id": "t",
            "type": "text",
            "bbox": [70, 50, 180, 55],
            "text": "中 AI 文",
            "style": {"font_size_pt": 96, "font_face": "PingFang SC", "color": "#111111"},
        }
        fit = object_fit.measure_object(
            obj, np.array(source), np.array(render), 400, 160, substituted={"PingFang SC"}
        )
        self.assertTrue(fit.get("spacing_remove"))
        object_fit.refine_page({"width_px": 400, "height_px": 160, "objects": [obj]}, [fit], {"PingFang SC"})
        self.assertEqual(obj["text"], "中AI文")

    def test_size_improvement_is_not_rolled_back_for_position_drift(self):
        import fidelity_loop

        old = {"id": "ratio-caption", "type": "text", "bbox": [10, 10, 200, 40], "style": {"font_size_pt": 24}}
        current = {"id": "ratio-caption", "type": "text", "bbox": [10, 10, 200, 40], "style": {"font_size_pt": 20}}
        payload = {"pages": [{"id": "slide-1", "objects": [current]}]}
        pending = {"slide-1:ratio-caption": {"object": old, "fit": {"status": "off", "scale_h": 1.02, "anchor_dx": 0}}}
        now = {"id": "ratio-caption", "status": "off", "scale_h": 1.01, "anchor_dx": 8.0}
        rolled, frozen, limits = fidelity_loop.apply_rollbacks(
            payload, [{"slide": 1, "object_fit": {"objects": [now]}}], pending, {}, set()
        )
        self.assertEqual(rolled, [])
        self.assertEqual(frozen, [])
        self.assertEqual(limits, {})
        self.assertEqual(current["style"]["font_size_pt"], 20)


class RenderCopyTests(unittest.TestCase):
    def test_compile_failure_is_reported(self):
        import tempfile
        from unittest import mock

        import render_compare

        payload = {
            "schema_version": "1.1",
            "pages": [{"id": "s", "width_px": 80, "height_px": 40, "objects": []}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "source.png"
            Image.new("RGB", (80, 40), WHITE).save(source)
            pptx = tmp / "deck.pptx"
            pptx.write_bytes(b"pptx")

            def fake_compile(payload, base_dir, output):
                raise ValueError("missing asset")

            def fake_render(pptx_path, outdir, dpi):
                outdir.mkdir(parents=True, exist_ok=True)
                rendered = outdir / "slide.png"
                Image.new("RGB", (80, 40), WHITE).save(rendered)
                return [rendered]

            with mock.patch.object(render_compare, "compile_payload", side_effect=fake_compile), mock.patch.object(
                render_compare, "render_slides", side_effect=fake_render
            ), mock.patch.object(render_compare, "substitute_payload", return_value=(payload, [])), mock.patch.object(
                render_compare, "font_report", return_value={"problems": [], "substitutions": []}
            ), mock.patch.object(render_compare, "slide_width_inches", return_value=10.0):
                report = render_compare.run_compare([source], pptx, tmp / "out", payload, write_images=False, base_dir=tmp)
        self.assertTrue(any("render copy compile failed" in p and "missing asset" in p for p in report["problems"]))

    def test_loop_passes_pageir_directory(self):
        import json
        import tempfile
        from unittest import mock

        import fidelity_loop

        payload = {
            "schema_version": "1.1",
            "pages": [{
                "id": "s",
                "width_px": 100,
                "height_px": 80,
                "objects": [{"id": "t", "type": "text", "bbox": [1, 1, 40, 20], "text": "a", "style": {"font_size_pt": 12}}],
            }],
        }
        report = {
            "object_fit_ratio": 1.0,
            "slides": [{"slide": 1, "normalized_mae": 0.0, "edge_f1": 1.0, "object_fit": {"objects": []}}],
            "problems": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            ir = tmp / "page.json"
            ir.write_text(json.dumps(payload), encoding="utf-8")
            Image.new("RGB", (100, 80), WHITE).save(tmp / "s.png")
            seen = []

            def fake_compile(payload, ir_path, pptx_path):
                pptx_path.write_bytes(b"pptx")

            def fake_compare(sources, pptx, outdir, payload, write_images=True, base_dir=None):
                outdir.mkdir(parents=True, exist_ok=True)
                seen.append(base_dir)
                return report

            argv = [
                "fidelity_loop.py", str(ir), str(tmp / "s.png"),
                "--out", str(tmp / "o.pptx"), "--workdir", str(tmp / "q"), "--rounds", "1",
            ]
            import io
            from contextlib import redirect_stdout

            with mock.patch.object(fidelity_loop, "compile_ir", side_effect=fake_compile), mock.patch.object(
                fidelity_loop, "run_compare", side_effect=fake_compare
            ), mock.patch("sys.argv", argv), redirect_stdout(io.StringIO()):
                rc = fidelity_loop.main()
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(seen), 2)
        self.assertTrue(all(path == ir.resolve().parent for path in seen))


if __name__ == "__main__":
    unittest.main()
