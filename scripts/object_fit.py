#!/usr/bin/env python3
"""Per-object fidelity measurement and correction.

For every PageIR object, find the visible ink of that object in the source
image and in the rendered slide (same source-pixel region) and compare them:

- shapes, lines, images: the four ink edges;
- text: line by line. The first line's glyph height drives font size, the
  line count drives wrapping, the line pitch drives line spacing, the line
  width drives fine font size / letter spacing, and the first line's anchor
  drives position.

`refine_pageir.py` applies the corrections; `fidelity_loop.py` repeats
compile -> render -> measure -> refine until objects match.
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_ops import INK_THRESHOLD, border_color, dominant_color, fill_region_background, ink_box, ink_lines, label_components  # noqa: E402
from pageir_common import pt_per_px  # noqa: E402


SIZE_TOLERANCE = 0.04  # glyph height
WIDTH_TOLERANCE = 0.015  # line width
PITCH_TOLERANCE = 0.03  # line spacing
FONT_FINE_TUNE_LIMIT = 0.06
MAX_PAGE_COVERAGE = 0.6
FONT_SCALE_LIMITS = (0.87, 1.15)
FONT_CUMULATIVE_LIMITS = (0.5, 1.6)  # of the round-1 size; 1.6em / scale_h guards stay separate
LINE_SPACING_LIMITS = (0.7, 2.5)
GROW_STEPS = (1.0, 2.0, 4.0)
TEXT_GROW_STEPS = (1.0, 2.0)  # text only; 4x pulls in the next card on the same row
WEIGHT_TOLERANCE = 0.15  # stroke ink density, source vs render
WEIGHT_INK_THRESHOLD = 60
SUBSTITUTED_WEIGHT_RATIO = 1.35
SUBSTITUTED_WEIGHT_RATIO_LOW = 0.65

CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z0-9]")
FULLWIDTH_PUNCTUATION = set("，。；：！？、（）【】《》「」『』“”‘’％·…—～")
SEPARATOR_PUNCTUATION = set("·/")


def position_tolerance(page_w: float) -> float:
    """Allowed ink displacement in source pixels: 2 px or 0.2% of the page width."""
    return max(2.0, 0.002 * page_w)


def text_of(obj: dict) -> str:
    if isinstance(obj.get("runs"), list):
        return "".join(str(r.get("text", "")) for r in obj["runs"])
    return str(obj.get("text") or "")


def object_region(obj: dict, page_w: float, page_h: float, grow: float = 1.0):
    """Search region [x0, y0, x1, y1] around an object, or None when it cannot be measured.

    `grow` widens the margin; measure_object retries with larger margins when an
    object has drifted out of, or is clipped by, the default search window.
    """
    if obj.get("rotation_deg"):
        return None
    if obj.get("type") == "line":
        pts = obj.get("points")
        if not isinstance(pts, list) or len(pts) != 4:
            return None
        x1, y1, x2, y2 = (float(v) for v in pts)
        pad = 12.0 * grow
        return [min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad]
    bbox = obj.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    x, y, w, h = (float(v) for v in bbox)
    if w * h > MAX_PAGE_COVERAGE * page_w * page_h:
        return None  # background-sized panels have no surrounding border to measure against
    if obj.get("type") == "text":
        # Margins from the font size, not the box: tall multi-line boxes would
        # otherwise reach out to an enclosing card. Vertically tighter, so the
        # next line of text above or below stays out.
        em = _em_px(obj, page_w, page_h)
        # Horizontal slack also covers single-line text that overflows its box.
        pad_x = max(4.0, 0.6 * em + 0.15 * w) * grow
        pad_y = max(3.0, 0.3 * em) * grow
        return [x - pad_x, y - pad_y, x + w + pad_x, y + h + pad_y]
    else:
        pad = max(6.0, 0.05 * min(w, h)) * grow
    return [x - pad, y - pad, x + w + pad, y + h + pad]


def _expand_text_mask(rect, own, pad: float) -> list[float]:
    """Expand another text box. Horizontal pad is full; vertical pad stops at half the gap."""
    x0, y0, x1, y1 = rect
    if own is None:
        return [x0 - pad, y0 - pad, x1 + pad, y1 + pad]
    ox0, oy0, ox1, oy1 = own
    x0 -= pad
    x1 += pad
    if y1 <= oy0:
        y1 += min(pad, (oy0 - y1) / 2.0)
        y0 -= pad
    elif y0 >= oy1:
        y0 -= min(pad, (y0 - oy1) / 2.0)
        y1 += pad
    else:
        y0 -= pad
        y1 += pad
    return [x0, y0, x1, y1]


def _em_px(obj: dict, page_w: float, page_h: float) -> float:
    style = obj.get("style") or {}
    sizes = [float(style.get("font_size_pt", 18))]
    sizes += [float(r["font_size_pt"]) for r in obj.get("runs") or [] if isinstance(r, dict) and "font_size_pt" in r]
    return max(sizes) / pt_per_px(page_w, page_h)


def _object_rect(obj: dict, page_w: float, page_h: float) -> list[float] | None:
    if obj.get("type") == "line" and isinstance(obj.get("points"), list):
        x1, y1, x2, y2 = (float(v) for v in obj["points"])
        pad = max(2.0, float((obj.get("style") or {}).get("width_pt", 1.0)) / pt_per_px(page_w, page_h))
        return [min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad]
    bbox = obj.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    x, y, w, h = (float(v) for v in bbox)
    return [x, y, x + w, y + h]

def _stroke_radius_px(obj: dict, page_w: float, page_h: float) -> float:
    """Half-width of a line's stroke band: the line width plus 2 px of fringe."""
    style = obj.get("style") or {}
    raw = style.get("width_pt", style.get("line_width_pt", 1.0))
    try:
        width_pt = float(raw)
    except (TypeError, ValueError):
        width_pt = 1.0
    return max(0.0, width_pt) / pt_per_px(page_w, page_h) + 2.0


def _paint_segment_band(mask: np.ndarray, obj: dict, page_w: float, page_h: float) -> None:
    """Paint the stroke band of a line onto a boolean mask."""
    pts = obj.get("points")
    if not isinstance(pts, list) or len(pts) != 4:
        return
    x1, y1, x2, y2 = (float(v) for v in pts)
    radius = _stroke_radius_px(obj, page_w, page_h)
    height, width = mask.shape
    x0 = max(0, int(np.floor(min(x1, x2) - radius)))
    x1i = min(width, int(np.ceil(max(x1, x2) + radius)) + 1)
    y0 = max(0, int(np.floor(min(y1, y2) - radius)))
    y1i = min(height, int(np.ceil(max(y1, y2) + radius)) + 1)
    if x1i <= x0 or y1i <= y0:
        return
    yy, xx = np.mgrid[y0:y1i, x0:x1i]
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 < 1e-6:
        dist = np.hypot(xx - x1, yy - y1)
    else:
        t = np.clip(((xx - x1) * dx + (yy - y1) * dy) / length2, 0.0, 1.0)
        dist = np.hypot(xx - (x1 + t * dx), yy - (y1 + t * dy))
    mask[y0:y1i, x0:x1i] |= dist <= radius


def _within_cumulative(target: float, baseline: float | None) -> bool:
    if baseline is None:
        return True
    low, high = FONT_CUMULATIVE_LIMITS
    return baseline * low - 1e-4 <= target <= baseline * high + 1e-4



def _measurement_mask(obj: dict, objects: list[dict], region, page_w: float, page_h: float) -> np.ndarray:
    """Mask other object ink from a text search window."""
    rx0, ry0, rx1, ry1 = region
    width, height = int(round(page_w)), int(round(page_h))
    mask = np.zeros((height, width), dtype=bool)
    own = _object_rect(obj, page_w, page_h)
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")):
            continue
        rect = _object_rect(other, page_w, page_h)
        if rect is None:
            continue
        if other.get("type") == "text":
            # Side ink sticks out of the neighbour's box. Expand horizontally by
            # 0.3em. Vertically, only take half the gap so a large title's em
            # does not erase the line we are measuring.
            pad = 0.3 * _em_px(other, page_w, page_h)
            rect = _expand_text_mask(rect, own, pad)
        if rect[0] >= rx1 or rect[2] <= rx0 or rect[1] >= ry1 or rect[3] <= ry0:
            continue
        x0, y0, x1, y1 = rect
        ix0, iy0 = max(0, int(np.floor(x0))), max(0, int(np.floor(y0)))
        ix1, iy1 = min(width, int(np.ceil(x1))), min(height, int(np.ceil(y1)))
        if ix1 <= ix0 or iy1 <= iy0:
            continue
        typ = other.get("type")
        if typ == "line":
            continue  # stroke band is painted after the text's own box is restored
        if typ in ("text", "image"):
            mask[iy0:iy1, ix0:ix1] = True
            continue
        style = other.get("style") or {}
        inside = own is not None and own[0] >= x0 and own[1] >= y0 and own[2] <= x1 and own[3] <= y1
        has_fill = style.get("fill") not in (None, "none", "transparent")
        if inside or not has_fill:
            band = max(4.0, float(style.get("line_width_pt", 1.0)) / pt_per_px(page_w, page_h) + 2.0)
            bx0, by0 = max(0, int(np.floor(x0 - band))), max(0, int(np.floor(y0 - band)))
            bx1, by1 = min(width, int(np.ceil(x1 + band))), min(height, int(np.ceil(y1 + band)))
            mask[by0:by1, bx0:bx1] = True
            iy0_in = min(by1, max(by0, int(np.ceil(y0 + band))))
            iy1_in = max(by0, min(by1, int(np.floor(y1 - band))))
            ix0_in = min(bx1, max(bx0, int(np.ceil(x0 + band))))
            ix1_in = max(bx0, min(bx1, int(np.floor(x1 - band))))
            if ix1_in > ix0_in and iy1_in > iy0_in:
                mask[iy0_in:iy1_in, ix0_in:ix1_in] = False
        else:
            mask[iy0:iy1, ix0:ix1] = True
    if own is not None:
        ox0, oy0, ox1, oy1 = own
        o_ix0, o_iy0 = max(0, int(np.floor(ox0))), max(0, int(np.floor(oy0)))
        o_ix1, o_iy1 = min(width, int(np.ceil(ox1))), min(height, int(np.ceil(oy1)))
        if o_ix1 > o_ix0 and o_iy1 > o_iy0:
            mask[o_iy0:o_iy1, o_ix0:o_ix1] = False
    # The text's own box is cleared above, which also revives a neighbour whose
    # box overlaps it (a title descending into the subtitle). Put that neighbour
    # box back. The expanded pad is not restored, so a large em cannot erase us.
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")) or other.get("type") != "text":
            continue
        rect = _object_rect(other, page_w, page_h)
        if rect is None:
            continue
        ix0, iy0 = max(0, int(np.floor(rect[0]))), max(0, int(np.floor(rect[1])))
        ix1, iy1 = min(width, int(np.ceil(rect[2]))), min(height, int(np.ceil(rect[3])))
        if ix1 > ix0 and iy1 > iy0:
            mask[iy0:iy1, ix0:ix1] = True
    # Tick marks cross the label box. The stroke band (line width + 2 px) is painted last.
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")) or other.get("type") != "line":
            continue
        _paint_segment_band(mask, other, page_w, page_h)
    # Clearing the text box above also revives a same-colored outline that crosses
    # it (the orange badge around HIGH). Put that stroke back, or it is counted
    # as another text line. Only the stroke ring is painted, never the interior.
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")):
            continue
        _paint_shape_stroke(mask, other, page_w, page_h)
    return mask


def _paint_shape_stroke(mask: np.ndarray, obj: dict, page_w: float, page_h: float) -> None:
    """Mask the stroke of a rect, round rect or ellipse, including rounded corners.

    The rectangular border band misses a large corner radius: the arc sits inside
    the box, clear of the band, and the text's own box then revives it. Signed
    distance follows the real outline. Interior fill is left alone.
    """
    if obj.get("type") not in ("rect", "round_rect", "ellipse"):
        return
    style = obj.get("style") or {}
    line = style.get("line")
    if not isinstance(line, str) or line.lower() in ("none", "transparent"):
        return
    bbox = obj.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return
    x, y, w, h = (float(v) for v in bbox)
    if w < 2 or h < 2:
        return
    try:
        width_pt = float(style.get("line_width_pt") or 1.0)
    except (TypeError, ValueError):
        width_pt = 1.0
    inward = max(1.0, width_pt) / pt_per_px(page_w, page_h) + 2.0
    outward = 2.0
    height, width = mask.shape
    x0 = max(0, int(np.floor(x - outward)))
    y0 = max(0, int(np.floor(y - outward)))
    x1 = min(width, int(np.ceil(x + w + outward)) + 1)
    y1 = min(height, int(np.ceil(y + h + outward)) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    yy, xx = np.mgrid[y0:y1, x0:x1]
    if obj.get("type") == "ellipse":
        nx = (xx - (x + w / 2.0)) / (w / 2.0)
        ny = (yy - (y + h / 2.0)) / (h / 2.0)
        sd = (np.hypot(nx, ny) - 1.0) * (min(w, h) / 2.0)
    else:
        radius = style.get("corner_radius_px") or 0.0
        try:
            radius = float(radius)
        except (TypeError, ValueError):
            radius = 0.0
        radius = max(0.0, min(radius, w / 2.0, h / 2.0))
        qx = np.abs(xx - (x + w / 2.0)) - w / 2.0 + radius
        qy = np.abs(yy - (y + h / 2.0)) - h / 2.0 + radius
        sd = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0)) + np.minimum(np.maximum(qx, qy), 0.0) - radius
    mask[y0:y1, x0:x1] |= (sd <= outward) & (sd >= -inward)


def _touches(box, region, page_w: float, page_h: float) -> bool:

    """True when ink reaches the search window edge (and the window is not at the page edge)."""
    rx0, ry0 = max(0.0, region[0]), max(0.0, region[1])
    rx1, ry1 = min(page_w, region[2]), min(page_h, region[3])
    return (
        (box[0] <= rx0 + 1 and rx0 > 0)
        or (box[1] <= ry0 + 1 and ry0 > 0)
        or (box[2] >= rx1 - 1 and rx1 < page_w)
        or (box[3] >= ry1 - 1 and ry1 < page_h)
    )


def _own_color(obj: dict) -> np.ndarray | None:
    """The color that identifies a shape's own pixels: its outline, else its fill."""
    style = obj.get("style") or {}
    value = None
    if obj.get("type") == "line":
        value = style.get("color", "#111111")
    elif obj.get("type") in ("rect", "round_rect", "ellipse", "triangle", "chevron"):
        value = style.get("line") if style.get("line") else style.get("fill")
    if not isinstance(value, str):
        return None
    from pageir_common import normalize_hex

    digits = normalize_hex(value)[1:]
    return np.array([int(digits[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.int32)


def color_box(image: np.ndarray, region, color: np.ndarray, anchor=None, pick: str = "union", tol_cap: float = 48.0):
    """Bounding box of pixels close to `color`.

    Contrast is against the window border, not the dominant color inside it:
    a filled pill or bar *is* the dominant color, and that used to reject it.
    `anchor` (the object's own rectangle, page pixels) keeps a same-colored
    neighbour out of the box. `pick="nearest"` returns the one component closest
    to that anchor (a line). `tol_cap` stops a nearby gray of a different object
    (a bar beside an axis) from matching.
    """
    h, w = image.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in region)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    crop = image[y0:y1, x0:x1, :3].astype(np.int32)
    bg = border_color(crop)
    contrast = float(np.abs(color - bg).max())
    if contrast < 10:
        return None
    tol = max(8.0, min(float(tol_cap), 0.5 * contrast))
    mask = np.abs(crop - color).max(axis=2) <= tol
    if mask.sum() < 4:
        return None
    comps = label_components(mask)
    if not comps:
        return None
    if pick == "along":
        if anchor is None:
            return None
        aw = max(1.0, anchor[2] - anchor[0])
        ah = max(1.0, anchor[3] - anchor[1])
        horizontal = aw > ah * 3
        reach = 14.0

        def near(cx: float, cy: float) -> bool:
            dx = 0.0 if anchor[0] <= cx <= anchor[2] else min(abs(cx - anchor[0]), abs(cx - anchor[2]))
            dy = 0.0 if anchor[1] <= cy <= anchor[3] else min(abs(cy - anchor[1]), abs(cy - anchor[3]))
            return dx * dx + dy * dy <= reach * reach

        sel = np.zeros(mask.shape, dtype=bool)
        for comp in comps:
            if comp["area"] < 6:
                continue
            bx, by, bw, bh = comp["bbox"]
            cx = bx + bw / 2
            cy = by + bh / 2
            if horizontal:
                if bh > max(6.0, bw) or not near(x0 + cx, y0 + cy):
                    continue
            elif bw > max(6.0, bh) or not near(x0 + cx, y0 + cy):
                continue
            sel[by : by + bh, bx : bx + bw] |= mask[by : by + bh, bx : bx + bw]
        if not sel.any():
            return None
        if horizontal:
            counts = sel.sum(axis=1)
            strong = counts >= max(2, 0.4 * float(counts.max()))
            sel[~strong, :] = False
        else:
            counts = sel.sum(axis=0)
            strong = counts >= max(2, 0.4 * float(counts.max()))
            sel[:, ~strong] = False
        if not sel.any():
            return None
        ys = np.flatnonzero(sel.any(axis=1))
        xs = np.flatnonzero(sel.any(axis=0))
        return [x0 + int(xs[0]), y0 + int(ys[0]), x0 + int(xs[-1]) + 1, y0 + int(ys[-1]) + 1]
    else:
        biggest = max(c["area"] for c in comps)

        def overlaps(comp) -> bool:
            if anchor is None:
                return True
            bx, by, bw, bh = comp["bbox"]
            return x0 + bx < anchor[2] and x0 + bx + bw > anchor[0] and y0 + by < anchor[3] and y0 + by + bh > anchor[1]

        # A glyph or a rounded cap can split the fill. Keep every piece that meets
        # the object and is more than a speck; otherwise the largest pieces.
        anchored = [c for c in comps if overlaps(c) and c["area"] >= max(6, 0.02 * biggest)]
        chosen = anchored or [c for c in comps if c["area"] >= 0.2 * biggest]
    if not chosen:
        return None
    keep = [c["bbox"] for c in chosen]
    bx0 = min(b[0] for b in keep)
    by0 = min(b[1] for b in keep)
    bx1 = max(b[0] + b[2] for b in keep)
    by1 = max(b[1] + b[3] for b in keep)
    return [x0 + bx0, y0 + by0, x0 + bx1, y0 + by1]


def _paint_rect(mask: np.ndarray, rect, pad: float = 0.0) -> None:
    height, width = mask.shape
    x0, y0, x1, y1 = rect
    ix0 = max(0, int(np.floor(x0 - pad)))
    iy0 = max(0, int(np.floor(y0 - pad)))
    ix1 = min(width, int(np.ceil(x1 + pad)))
    iy1 = min(height, int(np.ceil(y1 + pad)))
    if ix1 > ix0 and iy1 > iy0:
        mask[iy0:iy1, ix0:ix1] = True


def _fill_mask_with_border(image, region, mask: np.ndarray):
    """Paint masked pixels with the window border color, so a filled shape is not the fill color."""
    if not mask.any():
        return image
    out = image.copy()
    height, width = image.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in region)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return image
    bg = np.round(border_color(image[y0:y1, x0:x1, :3].astype(np.int32))).astype(out.dtype)
    view = mask[y0:y1, x0:x1]
    out[y0:y1, x0:x1][view] = bg
    return out


def _blank_foreign_for_shape(image, obj, objects, page_w, page_h):
    """Hide other text, filled shapes and line strokes before measuring a shape.

    A value label sitting on a bar, or the axis beside it, is close enough in
    gray to stretch the bar's color box. A shape that contains this one is the
    background and is left alone.
    """
    region = object_region(obj, page_w, page_h, GROW_STEPS[-1])
    if region is None:
        return image
    own = _object_rect(obj, page_w, page_h)
    mask = np.zeros(image.shape[:2], dtype=bool)
    rx0, ry0, rx1, ry1 = region
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")):
            continue
        kind = other.get("type")
        if kind == "line":
            pts = other.get("points")
            if not isinstance(pts, list) or len(pts) != 4:
                continue
            x1, y1, x2, y2 = (float(v) for v in pts)
            radius = _stroke_radius_px(other, page_w, page_h)
            if max(x1, x2) + radius <= rx0 or min(x1, x2) - radius >= rx1:
                continue
            if max(y1, y2) + radius <= ry0 or min(y1, y2) - radius >= ry1:
                continue
            _paint_segment_band(mask, other, page_w, page_h)
            continue
        rect = _object_rect(other, page_w, page_h)
        if rect is None:
            continue
        if rect[0] >= rx1 or rect[2] <= rx0 or rect[1] >= ry1 or rect[3] <= ry0:
            continue
        if own is not None and own[0] >= rect[0] and own[1] >= rect[1] and own[2] <= rect[2] and own[3] <= rect[3]:
            continue  # this shape sits inside the other; the other is the panel behind it
        if kind == "text":
            _paint_rect(mask, rect, 2.0)
            continue
        style = other.get("style") or {}
        if style.get("fill") not in (None, "none", "transparent"):
            _paint_rect(mask, rect)
    return _fill_mask_with_border(image, region, mask)


def _blank_other_lines(image, obj, objects, page_w, page_h):
    """Erase stroke bands of other lines that reach this line's search window.

    A tick and its axis share a color and a 9 px gap, so the color box unions
    them. Blanking the neighbours leaves the line being measured.
    """
    region = object_region(obj, page_w, page_h, GROW_STEPS[-1])
    if region is None:
        return image
    mask = np.zeros(image.shape[:2], dtype=bool)
    rx0, ry0, rx1, ry1 = region
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")) or other.get("type") != "line":
            continue
        pts = other.get("points")
        if not isinstance(pts, list) or len(pts) != 4:
            continue
        x1, y1, x2, y2 = (float(v) for v in pts)
        radius = _stroke_radius_px(other, page_w, page_h)
        if max(x1, x2) + radius <= rx0 or min(x1, x2) - radius >= rx1:
            continue
        if max(y1, y2) + radius <= ry0 or min(y1, y2) - radius >= ry1:
            continue
        _paint_segment_band(mask, other, page_w, page_h)
    if not mask.any():
        return image
    return fill_region_background(image, region, mask)


def _blank_foreign_for_line(image, obj, objects, page_w, page_h):
    """Hide PageIR-owned neighbouring objects before measuring a line.

    Same-colour icons and chart marks are otherwise indistinguishable from an
    arrow by RGB alone.  Keep an enclosing panel (the line is inside it), but
    erase every other object's bbox/stroke from the line search window.
    """
    region = object_region(obj, page_w, page_h, GROW_STEPS[-1])
    if region is None or not objects:
        return image
    own = _object_rect(obj, page_w, page_h)
    mask = np.zeros(image.shape[:2], dtype=bool)
    rx0, ry0, rx1, ry1 = region
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")):
            continue
        kind = other.get("type")
        if kind == "line":
            pts = other.get("points")
            if not isinstance(pts, list) or len(pts) != 4:
                continue
            x1, y1, x2, y2 = (float(v) for v in pts)
            radius = _stroke_radius_px(other, page_w, page_h)
            if max(x1, x2) + radius <= rx0 or min(x1, x2) - radius >= rx1:
                continue
            if max(y1, y2) + radius <= ry0 or min(y1, y2) - radius >= ry1:
                continue
            _paint_segment_band(mask, other, page_w, page_h)
            continue
        rect = _object_rect(other, page_w, page_h)
        if rect is None:
            continue
        if rect[0] >= rx1 or rect[2] <= rx0 or rect[1] >= ry1 or rect[3] <= ry0:
            continue
        # A panel/background containing the line is not a foreign mark.
        if own is not None and own[0] >= rect[0] and own[1] >= rect[1] and own[2] <= rect[2] and own[3] <= rect[3]:
            continue
        if kind in ("text", "image"):
            _paint_rect(mask, rect, 1.0)
        else:
            style = other.get("style") or {}
            if style.get("fill") not in (None, "none", "transparent"):
                _paint_rect(mask, rect)
            elif isinstance(style.get("line"), str):
                _paint_shape_stroke(mask, other, page_w, page_h)
    return _fill_mask_with_border(image, region, mask)


def _line_ink_box(image: np.ndarray, obj: dict, region, page_w: float, page_h: float) -> list[int] | None:
    """Return the full same-colour component connected to the line shaft."""
    color = _own_color(obj)
    if color is None:
        return None
    h, w = image.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in region)
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    crop = image[y0:y1, x0:x1, :3].astype(np.int32)
    tol = 32.0
    mask = np.abs(crop - color).max(axis=2) <= tol
    comps = [c for c in label_components(mask) if c["area"] >= 4]
    pts = obj.get("points")
    if not comps or not isinstance(pts, list) or len(pts) != 4:
        return None
    ax, ay, bx, by = (float(v) for v in pts)
    dx, dy = bx - ax, by - ay
    length = max(float(np.hypot(dx, dy)), 1.0)
    ux, uy = dx / length, dy / length
    radius = _stroke_radius_px(obj, page_w, page_h) + 4.0

    def segment_distance(cx: float, cy: float) -> float:
        t = max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / (length * length)))
        return float(np.hypot(cx - (ax + t * dx), cy - (ay + t * dy)))

    selected = []
    for comp in comps:
        cx = x0 + comp["bbox"][0] + comp["bbox"][2] / 2.0
        cy = y0 + comp["bbox"][1] + comp["bbox"][3] / 2.0
        # The component bbox must reach the shaft corridor. This admits both
        # chevron wings and the shaft while rejecting nearby disconnected icons.
        cbx, cby, cbw, cbh = comp["bbox"]
        gx0, gy0, gx1, gy1 = x0 + cbx, y0 + cby, x0 + cbx + cbw, y0 + cby + cbh
        corridor = False
        for t in (0.25, 0.5, 0.75):
            sx, sy = ax + t * dx, ay + t * dy
            if gx0 - radius <= sx <= gx1 + radius and gy0 - radius <= sy <= gy1 + radius:
                corridor = True
                break
        if corridor or segment_distance(cx, cy) <= radius:
            selected.append(comp)
    if not selected:
        return None
    boxes = [c["bbox"] for c in selected]
    bx0 = min(c[0] for c in boxes)
    by0 = min(c[1] for c in boxes)
    bx1 = max(c[0] + c[2] for c in boxes)
    by1 = max(c[1] + c[3] for c in boxes)
    return [x0 + bx0, y0 + by0, x0 + bx1, y0 + by1]


def _line_shaft_thickness(image: np.ndarray, obj: dict, page_w: float, page_h: float) -> float | None:
    """Median continuous ink length on perpendicular shaft cross-sections."""
    color = _own_color(obj)
    pts = obj.get("points")
    if color is None or not isinstance(pts, list) or len(pts) != 4:
        return None
    x1, y1, x2, y2 = (float(v) for v in pts)
    dx, dy = x2 - x1, y2 - y1
    length = float(np.hypot(dx, dy))
    if length < 8:
        return None
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    h, w = image.shape[:2]
    mask = np.abs(image[:, :, :3].astype(np.int32) - color).max(axis=2) <= 32.0
    half = int(max(8.0, min(24.0, 3.0 * _stroke_radius_px(obj, page_w, page_h))))
    samples = []
    for t in np.linspace(0.30, 0.70, 5):
        cx, cy = x1 + t * dx, y1 + t * dy
        vals = []
        for offset in np.arange(-half, half + 0.51, 0.5):
            xx, yy = int(round(cx + px * offset)), int(round(cy + py * offset))
            vals.append(0 <= xx < w and 0 <= yy < h and bool(mask[yy, xx]))
        runs = []
        start = None
        for i, hit in enumerate(vals + [False]):
            if hit and start is None:
                start = i
            elif not hit and start is not None:
                runs.append((start, i))
                start = None
        if runs:
            centre = len(vals) / 2.0
            run = min(runs, key=lambda r: abs((r[0] + r[1]) / 2.0 - centre))
            samples.append((run[1] - run[0]) * 0.5)
    return float(np.median(samples)) if samples else None


def _measure_box(obj, source, rendered, page_w, page_h, objects=None):
    src = ren = None
    color = _own_color(obj)
    anchor = _object_rect(obj, page_w, page_h)
    kind = obj.get("type")
    if objects and kind == "line":
        source = _blank_foreign_for_line(source, obj, objects, page_w, page_h)
        rendered = _blank_foreign_for_line(rendered, obj, objects, page_w, page_h)
    elif objects and kind not in ("text", "line"):
        source = _blank_foreign_for_shape(source, obj, objects, page_w, page_h)
        rendered = _blank_foreign_for_shape(rendered, obj, objects, page_w, page_h)
    # A line's gray is close to a bar's gray. Cap the match, and take the one
    # component nearest the line so the bar (or a tick further along the axis)
    # cannot stretch the box. If the color matches on only one side, do not
    # fall back to "any dark pixel": that swallows nearby labels.
    has_arrow = kind == "line" and (
        (obj.get("style") or {}).get("end_arrow") not in (None, "none")
        or (obj.get("style") or {}).get("start_arrow") not in (None, "none")
    )
    if kind == "line" and has_arrow:
        for grow in GROW_STEPS:
            region = object_region(obj, page_w, page_h, grow)
            src = _line_ink_box(source, obj, region, page_w, page_h)
            ren = _line_ink_box(rendered, obj, region, page_w, page_h)
            if src is not None and ren is not None and not (_touches(src, region, page_w, page_h) or _touches(ren, region, page_w, page_h)):
                return src, ren
        return src, ren
    pick = "along" if kind == "line" else "union"
    tol_cap = 20.0 if kind == "line" else 48.0
    for grow in GROW_STEPS:
        region = object_region(obj, page_w, page_h, grow)
        src_c = ren_c = None
        if color is not None:
            src_c = color_box(source, region, color, anchor, pick=pick, tol_cap=tol_cap)
            ren_c = color_box(rendered, region, color, anchor, pick=pick, tol_cap=tol_cap)
        explicit_color = isinstance((obj.get("style") or {}).get("color") if kind == "line" else None, str)
        if kind != "line":
            explicit_color = False
        if src_c is None and ren_c is None and not explicit_color:
            # No usable color (or the default black missed an uncolored line): any ink.
            src = ink_box(source, region)
            ren = ink_box(rendered, region)
        else:
            # A colored line that is not there must not be replaced by nearby text.
            # A wider window that misses must not wipe a hit from a tighter one.
            if src_c is not None or src is None:
                src = src_c
            if ren_c is not None or ren is None:
                ren = ren_c
        if src is not None and ren is not None and not (
            _touches(src, region, page_w, page_h) or _touches(ren, region, page_w, page_h)
        ):
            break
    return src, ren


def _own_lines(lines, obj, page_w, page_h):
    """Lines whose vertical centre lies within the text box (plus a little slack).

    Keeps a neighbouring paragraph that reaches into the search window from
    being measured as part of this object. Falls back to every line when none
    sit in the slack, so callers must still apply `_lines_in_vertical_band`.
    """
    x, y, w, h = (float(v) for v in obj["bbox"])
    slack = 0.3 * _em_px(obj, page_w, page_h)
    own = [b for b in lines if y - slack <= (b[1] + b[3]) / 2 <= y + h + slack]
    return own or lines


def _lines_in_vertical_band(lines, obj, page_w, page_h):
    """Drop lines whose centre is more than 0.5em outside the text box.

    Unlike `_own_lines` this has no fallback, so an icon base above the box
    cannot become the first line when the slack search missed.
    """
    y, h = float(obj["bbox"][1]), float(obj["bbox"][3])
    limit = 0.5 * _em_px(obj, page_w, page_h)
    return [b for b in lines if y - limit <= (b[1] + b[3]) / 2 <= y + h + limit]


def _column_clusters(image, line, bg_box) -> list[tuple[int, int]]:
    """Contiguous ink runs along one text line, as page-x intervals [start, end)."""
    height, width = image.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in line)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        return []
    bx0, by0, bx1, by1 = (int(round(v)) for v in bg_box)
    bx0, by0 = max(0, bx0), max(0, by0)
    bx1, by1 = min(width, bx1), min(height, by1)
    behind = image[by0:by1, bx0:bx1, :3]
    if behind.size == 0:
        return []
    bg = dominant_color(behind)
    crop = image[y0:y1, x0:x1, :3].astype(np.int32)
    columns = (np.abs(crop - bg).max(axis=2) > INK_THRESHOLD).any(axis=0)
    clusters: list[tuple[int, int]] = []
    start = None
    for index, on in enumerate(columns.tolist()):
        if on and start is None:
            start = index
        elif not on and start is not None:
            clusters.append((x0 + start, x0 + index))
            start = None
    if start is not None:
        clusters.append((x0 + start, x0 + len(columns)))
    return clusters


def _spacing_boundaries(text: str) -> list[tuple[int, int]]:
    """Return whitespace spans whose adjacent characters should be gap-checked."""
    boundaries = []
    for match in re.finditer(r"\s+", text):
        left = text[: match.start()].rstrip()[-1:]
        right = text[match.end() :].lstrip()[:1]
        if not left or not right:
            continue
        left_cjk, right_cjk = bool(CJK_RE.fullmatch(left)), bool(CJK_RE.fullmatch(right))
        left_lat, right_lat = bool(LATIN_RE.fullmatch(left)), bool(LATIN_RE.fullmatch(right))
        punctuation = (
            left in FULLWIDTH_PUNCTUATION
            or right in FULLWIDTH_PUNCTUATION
            or left in SEPARATOR_PUNCTUATION
            or right in SEPARATOR_PUNCTUATION
        )
        if (left_cjk and right_lat) or (left_lat and right_cjk) or punctuation:
            boundaries.append((match.start(), match.end()))
    return boundaries


def _boundary_gap(image, line, text: str, span: tuple[int, int], bg_box) -> float | None:
    """Approximate one source/render space gap by character-order projection."""
    clusters = _column_clusters(image, line, bg_box)
    if len(clusters) < 2:
        return None
    nonspace = [ch for ch in text if not ch.isspace()]
    if len(nonspace) < 2:
        return None
    left_count = sum(1 for ch in text[: span[0]] if not ch.isspace())
    if left_count <= 0 or left_count >= len(nonspace):
        return None
    # Character-order alignment is deliberately independent of font metrics.
    left_idx = min(len(clusters) - 2, round((left_count - 1) * (len(clusters) - 1) / (len(nonspace) - 1)))
    right_idx = min(len(clusters) - 1, round(left_count * (len(clusters) - 1) / (len(nonspace) - 1)))
    if right_idx <= left_idx:
        right_idx = left_idx + 1
    return max(0.0, float(clusters[right_idx][0] - clusters[left_idx][1]))


def _needs_spacing_removal(
    text: str,
    src_image,
    ren_image,
    src_line,
    ren_line,
    bg_box,
    source_em: float = 1.0,
    render_em: float = 1.0,
) -> list[tuple[int, int]]:
    """Compare source/render boundary gaps after normalizing each to its own em.

    A real separator such as ``·`` or ``/`` is allowed to retain its spaces:
    when the source gap is at least a quarter of the source em, removing it
    would turn intentional typography into a false correction.
    """
    boundaries = _spacing_boundaries(text)
    source_em = max(float(source_em), 1e-6)
    render_em = max(float(render_em), 1e-6)
    separators = {"·", "/"}
    removals = []
    for span in boundaries:
        src_gap = _boundary_gap(src_image, src_line, text, span, bg_box)
        ren_gap = _boundary_gap(ren_image, ren_line, text, span, bg_box)
        if src_gap is None or ren_gap is None:
            continue
        left = text[: span[0]].rstrip()[-1:]
        right = text[span[1] :].lstrip()[:1]
        if (left in separators or right in separators) and src_gap / source_em >= 0.25:
            continue
        if src_gap / source_em < 0.6 * (ren_gap / render_em):
            removals.append(span)
    return removals


def _merge_gap_clusters(clusters, x0: float, x1: float, gap_max: float) -> list[tuple[int, int]]:
    """Keep clusters that meet the object box, plus neighbours across a small gap.

    Clusters separated by more than `gap_max` stay out: they are the next
    phrase on the same row, not this object.
    """
    if not clusters:
        return []
    parent = list(range(len(clusters)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    ordered = sorted(range(len(clusters)), key=lambda i: clusters[i][0])
    for left, right in zip(ordered, ordered[1:]):
        if clusters[right][0] - clusters[left][1] <= gap_max:
            union(left, right)
    seeds = [i for i, (a, b) in enumerate(clusters) if b > x0 and a < x1]
    if not seeds:
        # Glyphs that sit just outside the box (a right-aligned label whose ink
        # starts past the box edge) still belong to it. Do not reach a phrase
        # that is already more than gap_max away.
        def gap(i: int) -> float:
            a, b = clusters[i]
            if b < x0:
                return x0 - b
            if a > x1:
                return a - x1
            return 0.0

        nearest = min(range(len(clusters)), key=gap)
        if gap(nearest) <= gap_max:
            seeds = [nearest]
    if not seeds:
        return []
    roots = {find(i) for i in seeds}
    return [clusters[i] for i in ordered if find(i) in roots]


def _limit_line_span(image, line, obj, page_w, page_h, bg_box):
    """Trim one line to the glyph cluster that belongs to this text object."""
    x, _y, w, _h = (float(v) for v in obj["bbox"])
    clusters = _column_clusters(image, line, bg_box)
    em = _em_px(obj, page_w, page_h)
    # 0.9em connects glyphs of one word. Cap it by the box width so a huge em
    # (the 1:1 numerals) does not swallow the other numeral on the same row.
    gap_max = min(0.9 * em, max(18.0, 0.45 * w))
    kept = _merge_gap_clusters(clusters, x, x + w, gap_max)
    if not kept:
        return None
    return [min(a for a, _b in kept), line[1], max(b for _a, b in kept), line[3]]


def _select_text_lines(image, lines, obj, page_w, page_h, bg_box):
    selected = []
    band = _lines_in_vertical_band(_own_lines(lines, obj, page_w, page_h), obj, page_w, page_h)
    for line in band:
        trimmed = _limit_line_span(image, line, obj, page_w, page_h, bg_box)
        if trimmed is not None:
            selected.append(trimmed)
    return selected


def _side_clipped(image, region, lines, bg_box, page_w) -> bool:
    """True when glyph ink on these lines continues past the window's left/right edge."""
    if not lines:
        return False
    h, w = image.shape[:2]
    x0, x1 = int(max(0, region[0])), int(min(page_w, region[2]))
    y0 = max(0, int(min(b[1] for b in lines)))
    y1 = min(h, int(max(b[3] for b in lines)))
    bx0, by0, bx1, by1 = (int(round(v)) for v in bg_box)
    behind = image[max(0, by0) : min(h, by1), max(0, bx0) : min(w, bx1), :3]
    if behind.size == 0 or y1 <= y0:
        return False
    bg = dominant_color(behind)
    for cols, at_page_edge in ((slice(x0, x0 + 2), x0 <= 0), (slice(max(x1 - 2, 0), x1), x1 >= page_w)):
        strip = image[y0:y1, cols, :3].astype(np.int32)
        if not at_page_edge and strip.size and (np.abs(strip - bg).max(axis=2) > 48).any():
            return True
    return False


def _soften_line_mask(mask, image, obj, objects, page_w, page_h):
    """Keep a misplaced gray tick from erasing a dark glyph it crosses.

    The geometric stroke band stays for a line whose color is close to the text
    (it really is the stroke). A gray axis or tick only blanks pixels that are
    actually that gray, so the glyph underneath survives.
    """
    text_color = _text_color(obj)
    if text_color is None:
        return mask
    out = mask.copy()
    for other in objects:
        if other is obj or str(other.get("id")) == str(obj.get("id")) or other.get("type") != "line":
            continue
        line_color = _own_color(other)
        if line_color is None or float(np.abs(line_color - text_color).max()) < 48:
            continue
        band = np.zeros(mask.shape, dtype=bool)
        _paint_segment_band(band, other, page_w, page_h)
        if not band.any():
            continue
        out[band] = False
        ys, xs = np.nonzero(band)
        pix = image[ys, xs, :3].astype(np.int32)
        d_line = np.abs(pix - line_color).max(axis=1)
        d_text = np.abs(pix - text_color).max(axis=1)
        keep = (d_line <= 32) & (d_line + 8 < d_text)
        out[ys[keep], xs[keep]] = True
    return out


def _trim_color_fringe(image, lines, color, core: float = 40.0):
    """Drop edge rows that only match the text color through a light fringe.

    A 24px glyph and a 25px glyph are 4% apart, which fails the size gate, when
    the extra row is antialiasing (no pixel within `core` of the text color).
    """
    if color is None or not lines:
        return lines
    height, width = image.shape[:2]
    trimmed = []
    for box in lines:
        x0, y0, x1, y1 = (int(v) for v in box)
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(width, x1), min(height, y1)
        if x1 <= x0 or y1 <= y0:
            continue

        def dark(y: int) -> bool:
            row = image[y, x0:x1, :3].astype(np.int32)
            return bool((np.abs(row - color).max(axis=1) <= core).any())

        while y0 < y1 - 1 and not dark(y0):
            y0 += 1
        while y1 - 1 > y0 and not dark(y1 - 1):
            y1 -= 1
        trimmed.append([x0, y0, max(x1, x0 + 1), y1])
    return trimmed


def _drop_slivers(lines, obj, page_w, page_h):
    """Drop a 1px fringe that merged-line detection left above the real glyphs."""
    if len(lines) < 2:
        return lines
    em = _em_px(obj, page_w, page_h)
    floor = max(4.0, 0.35 * em)
    kept = [b for b in lines if b[3] - b[1] >= floor]
    return kept or lines


def _line_height_plausible(lines, obj, page_w, page_h) -> bool:
    if not lines:
        return False
    height = lines[0][3] - lines[0][1]
    em = _em_px(obj, page_w, page_h)
    return 0.45 * em <= height <= 1.6 * em


def _measure_lines(obj, source, rendered, page_w, page_h, objects=None):
    src = ren = []
    src_image, ren_image = source, rendered
    x, y, w, h = (float(v) for v in obj["bbox"])
    bg_box = [x, y, x + w, y + h]
    color = _text_color(obj)
    for grow in TEXT_GROW_STEPS:
        region = object_region(obj, page_w, page_h, grow)
        src_image, ren_image = source, rendered
        if objects:
            src_mask = _soften_line_mask(
                _measurement_mask(obj, objects, region, page_w, page_h), source, obj, objects, page_w, page_h
            )
            ren_mask = _soften_line_mask(
                _measurement_mask(obj, objects, region, page_w, page_h), rendered, obj, objects, page_w, page_h
            )
            src_image = fill_region_background(source, region, src_mask, bg_region=bg_box)
            ren_image = fill_region_background(rendered, region, ren_mask, bg_region=bg_box)
        # Own-color ink ignores the axis, the bar and a neighbour in another color.
        # Any-dark ink fills a side the color missed, or replaces an implausible hit.
        src = _color_line_boxes(src_image, region, color) if color is not None else []
        ren = _color_line_boxes(ren_image, region, color) if color is not None else []
        src = _trim_color_fringe(src_image, src, color)
        ren = _trim_color_fringe(ren_image, ren, color)
        src = _drop_slivers(src, obj, page_w, page_h)
        ren = _drop_slivers(ren, obj, page_w, page_h)
        if not _line_height_plausible(src, obj, page_w, page_h):
            src = []
        if not _line_height_plausible(ren, obj, page_w, page_h):
            ren = []
        if not src or not ren:
            ink_src = ink_lines(src_image, region, bg_region=bg_box)
            ink_ren = ink_lines(ren_image, region, bg_region=bg_box)
            src = src or ink_src
            ren = ren or ink_ren
        clipped = _side_clipped(src_image, region, src, bg_box, page_w) or _side_clipped(ren_image, region, ren, bg_box, page_w)
        if src and ren and not clipped:
            break
    return (
        _drop_slivers(_select_text_lines(src_image, src, obj, page_w, page_h, bg_box), obj, page_w, page_h),
        _drop_slivers(_select_text_lines(ren_image, ren, obj, page_w, page_h, bg_box), obj, page_w, page_h),
    )


def ink_fraction(image: np.ndarray, band, bg_box) -> float | None:
    """Share of a text-line band covered by glyph ink: a proxy for stroke weight.

    The background is the dominant color inside the text box, as for line finding.
    """
    h, w = image.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in band)
    inner = image[y0:y1, x0:x1, :3].astype(np.int32)
    bx0, by0, bx1, by1 = (int(round(v)) for v in bg_box)
    behind = image[max(0, by0) : min(h, by1), max(0, bx0) : min(w, bx1), :3]
    if inner.size == 0 or behind.size == 0:
        return None
    mask = np.abs(inner - dominant_color(behind)).max(axis=2) > WEIGHT_INK_THRESHOLD
    return float(mask.mean())


def bold_state(obj: dict) -> bool | None:
    """Object-level boldness, or None when runs set their own weight (mixed emphasis)."""
    if any(isinstance(r, dict) and "bold" in r for r in obj.get("runs") or []):
        return None
    return bool((obj.get("style") or {}).get("bold"))


def _union(lines):
    return [min(b[0] for b in lines), min(b[1] for b in lines), max(b[2] for b in lines), max(b[3] for b in lines)]



def _rgb_of(value: str) -> np.ndarray | None:
    from pageir_common import normalize_hex

    try:
        digits = normalize_hex(value)[1:]
    except ValueError:
        return None
    return np.array([int(digits[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.int32)


def _text_color(obj: dict) -> np.ndarray | None:
    style = obj.get("style") or {}
    if isinstance(style.get("color"), str):
        return _rgb_of(style["color"])
    for run in obj.get("runs") or []:
        if isinstance(run, dict) and isinstance(run.get("color"), str):
            found = _rgb_of(run["color"])
            if found is not None:
                return found
    return None


def _merge_line_boxes(boxes: list[list[int]]) -> list[list[int]]:
    lines: list[list[int]] = []
    for box in sorted(boxes, key=lambda item: item[1]):
        height = max(1, box[3] - box[1])
        for line in lines:
            overlap = min(box[3], line[3]) - max(box[1], line[1])
            gap = max(box[1], line[1]) - min(box[3], line[3])
            horiz = min(box[2], line[2]) - max(box[0], line[0])
            touches = gap <= 2 and horiz > 0.5 * min(box[2] - box[0], line[2] - line[0])
            if overlap > 0.3 * min(height, line[3] - line[1]) or touches:
                line[0] = min(line[0], box[0])
                line[1] = min(line[1], box[1])
                line[2] = max(line[2], box[2])
                line[3] = max(line[3], box[3])
                break
        else:
            lines.append(list(box))
    return lines


def _color_line_boxes(image, region, color: np.ndarray) -> list[list[int]]:
    """Line boxes of pixels close to a text color, inside a search window."""
    height, width = image.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in region)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return []
    crop = image[y0:y1, x0:x1, :3].astype(np.int32)
    contrast = float(np.abs(color - border_color(crop)).max())
    if contrast < 10:
        return []
    tol = max(8.0, min(48.0, 0.5 * contrast))
    mask = np.abs(crop - color).max(axis=2) <= tol
    if int(mask.sum()) < 4:
        return []
    boxes = []
    for comp in label_components(mask):
        if comp["area"] < 4:
            continue
        bx, by, bw, bh = comp["bbox"]
        boxes.append([x0 + bx, y0 + by, x0 + bx + bw, y0 + by + bh])
    return _merge_line_boxes(boxes)


def _pick_line(lines: list[list[int]], target) -> list[int] | None:
    if not lines or not target:
        return None

    def dist(box: list[int]) -> float:
        return ((box[0] + box[2]) / 2 - (target[0] + target[2]) / 2) ** 2 + (
            (box[1] + box[3]) / 2 - (target[1] + target[3]) / 2
        ) ** 2

    return min(lines, key=dist)


def _recover_color_lines(obj, source, rendered, page_w, page_h):
    """4x window, source and render ink of this text's own color.

    Used when the ordinary line finder reports extra_in_render or
    missing_in_render: the glyphs have drifted out of the text box, so the
    box-overlap filter drops them.
    """
    color = _text_color(obj)
    region = object_region(obj, page_w, page_h, 4.0)
    anchor = _object_rect(obj, page_w, page_h)
    if color is None or region is None or anchor is None:
        return None, None
    return (
        _pick_line(_color_line_boxes(source, region, color), anchor),
        _pick_line(_color_line_boxes(rendered, region, color), anchor),
    )


def measure_text(
    obj: dict, source, rendered, page_w: float, page_h: float, result: dict, check_width: bool = True,
    objects: list[dict] | None = None
) -> dict:
    src_lines, ren_lines = _measure_lines(obj, source, rendered, page_w, page_h, objects)
    if not src_lines or not ren_lines:
        color_src, color_ren = _recover_color_lines(obj, source, rendered, page_w, page_h)
        if not src_lines and color_src:
            src_lines = [color_src]
        if not ren_lines and color_ren:
            ren_lines = [color_ren]
    if not src_lines and not ren_lines:
        result["status"] = "no_ink"
        return result
    if not ren_lines:
        result.update(status="missing_in_render", source_ink=_union(src_lines))
        return result
    if not src_lines:
        result.update(status="extra_in_render", render_ink=_union(ren_lines))
        return result

    tol = position_tolerance(page_w)
    s0, r0 = src_lines[0], ren_lines[0]
    scale_h = (s0[3] - s0[1]) / max(r0[3] - r0[1], 1)
    suspect = (s0[3] - s0[1]) > 1.6 * _em_px(obj, page_w, page_h) or not 0.5 <= scale_h <= 2.0
    align = (obj.get("style") or {}).get("align", "left")
    if align == "left":
        anchor_dx = s0[0] - r0[0]
    elif align == "right":
        anchor_dx = s0[2] - r0[2]
    else:
        anchor_dx = (s0[0] + s0[2] - r0[0] - r0[2]) / 2
    anchor_dy = (s0[1] + s0[3] - r0[1] - r0[3]) / 2
    # Widths are only comparable line-for-line when the source has no soft
    # wraps (each visual line is one logical line); compare the first line.
    logical_lines = text_of(obj).split("\n")
    same_lines = len(src_lines) == len(ren_lines)
    width_comparable = same_lines and len(src_lines) == len(logical_lines)
    src_w = s0[2] - s0[0]
    ren_w = r0[2] - r0[0]
    pitch_ratio = None
    if same_lines and len(src_lines) > 1:
        pitch_ratio = (src_lines[-1][1] - s0[1]) / max(ren_lines[-1][1] - r0[1], 1)
    result.update(
        {
            "source_ink": _union(src_lines),
            "render_ink": _union(ren_lines),
            "source_lines": len(src_lines),
            "render_lines": len(ren_lines),
            "first_line_source": s0,
            "first_line_render": r0,
            "anchor_dx": round(float(anchor_dx), 1),
            "anchor_dy": round(float(anchor_dy), 1),
            "scale_h": round(scale_h, 4),
            "source_width": int(src_w),
            "render_width": int(ren_w),
            "width_comparable": width_comparable,
            "pitch_ratio": round(pitch_ratio, 4) if pitch_ratio is not None else None,
        }
    )
    spacing_spans = []
    # Do not infer whitespace corrections while a font-size correction is
    # still needed: source and render ems are not comparable in that state.
    if same_lines and abs(scale_h - 1) <= 2 * SIZE_TOLERANCE:
        render_em = _em_px(obj, page_w, page_h)
        source_em = render_em * scale_h
        spacing_spans = _needs_spacing_removal(
            text_of(obj),
            source,
            rendered,
            s0,
            r0,
            bg_box=[
                float(obj["bbox"][0]),
                float(obj["bbox"][1]),
                float(obj["bbox"][0] + obj["bbox"][2]),
                float(obj["bbox"][1] + obj["bbox"][3]),
            ],
            source_em=source_em,
            render_em=render_em,
        )
    if spacing_spans:
        result["spacing_remove"] = True
        result["spacing_spans"] = spacing_spans
    if suspect:
        result.update(status="off", suspect="measurement")
        return result
    # Substituted fonts cannot be compared at ordinary ratios, but an extreme
    # density mismatch is strong enough evidence to toggle an explicitly wrong
    # bold state. Small glyphs remain unmeasured.
    weight_fix = None
    weight_evidence = False
    # Under 16 px the stroke-density ratio is noise, not a weight.
    glyph_h = s0[3] - s0[1]
    if glyph_h >= 16 and abs(scale_h - 1) <= 0.1:
        bx, by, bw, bh = (float(v) for v in obj["bbox"])
        weight_box = [bx, by, bx + bw, by + bh]
        src_ink, ren_ink = ink_fraction(source, s0, weight_box), ink_fraction(rendered, r0, weight_box)
        if src_ink and ren_ink:
            ratio = src_ink / ren_ink
            bold = bold_state(obj)
            extreme = ratio >= SUBSTITUTED_WEIGHT_RATIO or ratio <= SUBSTITUTED_WEIGHT_RATIO_LOW
            if check_width or extreme:
                result["weight_ratio"] = round(ratio, 3)
            if not check_width and not extreme:
                result["weight_unverified"] = True
            else:
                weight_evidence = True
            if ratio >= (SUBSTITUTED_WEIGHT_RATIO if not check_width else 1 + WEIGHT_TOLERANCE):
                if bold is False:
                    weight_fix = "bold"
                elif bold is True:
                    result["font_hint"] = "source strokes are heavier than this font's bold; use a heavier family/weight"
            elif ratio <= (SUBSTITUTED_WEIGHT_RATIO_LOW if not check_width else 1 - WEIGHT_TOLERANCE):
                if bold is True:
                    weight_fix = "regular"
                elif bold is False:
                    result["font_hint"] = "source strokes are lighter than this font's regular; use a lighter weight"
    if not check_width and not weight_evidence and "weight_unverified" not in result:
        result["weight_unverified"] = True
    if check_width or "weight_ratio" in result:
        result["weight_fix"] = weight_fix
    ok = (
        same_lines
        and weight_fix is None
        and abs(anchor_dx) <= tol
        and abs(anchor_dy) <= tol
        and abs(scale_h - 1) <= SIZE_TOLERANCE
        and (not width_comparable or not check_width or abs(src_w - ren_w) <= max(2 * tol, WIDTH_TOLERANCE * src_w))
        and (pitch_ratio is None or abs(pitch_ratio - 1) <= PITCH_TOLERANCE)
    )
    if not check_width:
        # Rendered with a substitute font: line length says nothing about the user's font.
        result["width_unverified"] = True
    result["status"] = "ok" if ok else "off"
    return result


def measure_object(
    obj: dict,
    source: np.ndarray,
    rendered: np.ndarray,
    page_w: float,
    page_h: float,
    substituted: set[str] | None = None,
    default_font: str = "Arial",
    objects: list[dict] | None = None,
) -> dict:
    result: dict = {"id": obj.get("id"), "type": obj.get("type")}
    if object_region(obj, page_w, page_h) is None:
        result["status"] = "not_measured"
        return result
    if obj.get("type") == "text":
        check_width = not uses_font(obj, substituted or set(), default_font)
        return measure_text(obj, source, rendered, page_w, page_h, result, check_width, objects)

    # A separate arrow triangle is the same color as the shaft, so the color
    # box already includes it in the ink extent.
    src, ren = _measure_box(obj, source, rendered, page_w, page_h, objects)
    if src is None and ren is None:
        result["status"] = "no_ink"
        return result
    if src is None:
        result.update(status="extra_in_render", render_ink=ren)
        return result
    if ren is None:
        result.update(status="missing_in_render", source_ink=src)
        return result
    tol = position_tolerance(page_w)
    edges = {"dx0": src[0] - ren[0], "dy0": src[1] - ren[1], "dx1": src[2] - ren[2], "dy1": src[3] - ren[3]}
    result.update({"source_ink": src, "render_ink": ren, **edges})
    result["status"] = "ok" if all(abs(v) <= tol for v in edges.values()) else "off"
    if obj.get("type") == "line":
        src_width = _line_shaft_thickness(source, obj, page_w, page_h)
        ren_width = _line_shaft_thickness(rendered, obj, page_w, page_h)
        if src_width is not None and ren_width is not None:
            result["stroke_width_source_px"] = round(src_width, 2)
            result["stroke_width_render_px"] = round(ren_width, 2)
            result["stroke_width_delta_px"] = round(src_width - ren_width, 2)
            if abs(src_width - ren_width) > 1.0:
                result["status"] = "off"
    if obj.get("type") == "line" and (
        (obj.get("style") or {}).get("end_arrow") not in (None, "none")
        or (obj.get("style") or {}).get("start_arrow") not in (None, "none")
    ):
        try:
            from detect_shapes import measure_arrow_head

            style = obj.get("style") or {}
            color = style.get("color", "#111111")
            stroke_px = float(style.get("width_pt", 1.0)) / pt_per_px(page_w, page_h)
            src_head = measure_arrow_head(source, obj.get("points"), color, stroke_px)
            ren_head = measure_arrow_head(rendered, obj.get("points"), color, stroke_px)
            if src_head:
                result["head_w_source"] = src_head["size"]
                result["head_len_source"] = src_head["size"]
                result["head_w_source_px"] = src_head.get("head_w_px")
                result["head_len_source_px"] = src_head.get("head_len_px")
                result["head_kind_source"] = src_head.get("kind")
                result["head_fill_source"] = src_head.get("fill_ratio")
            if ren_head:
                result["head_w_render"] = ren_head["size"]
                result["head_len_render"] = ren_head["size"]
                result["head_w_render_px"] = ren_head.get("head_w_px")
                result["head_len_render_px"] = ren_head.get("head_len_px")
                result["head_kind_render"] = ren_head.get("kind")
                result["head_fill_render"] = ren_head.get("fill_ratio")
            if _head_class_mismatch_is_actionable(style, (src_head or {}).get("size"), (ren_head or {}).get("size")):
                result["status"] = "off"
            if src_head and ren_head and src_head.get("kind") != ren_head.get("kind"):
                result["head_kind_mismatch"] = True
                result["status"] = "off"
        except (ImportError, ValueError):
            pass
    return result



def _head_class_mismatch_is_actionable(style: dict, src_size, ren_size) -> bool:
    """Any measurable preset-class mismatch is actionable; ``lg`` is no waiver."""
    order = ("sm", "med", "lg")
    return src_size in order and ren_size in order and src_size != ren_size


def object_error(f: dict) -> float:
    keys = ("dx0", "dy0", "dx1", "dy1", "anchor_dx", "anchor_dy")
    err = max([abs(f.get(k) or 0) for k in keys] + [0])
    if f.get("scale_h"):
        err = max(err, abs(f["scale_h"] - 1) * 100)
    if f["status"] in ("missing_in_render", "extra_in_render"):
        err = float("inf")
    return err


def measure_page(
    objects: list[dict],
    source: np.ndarray,
    rendered: np.ndarray,
    substituted: set[str] | None = None,
    default_font: str = "Arial",
    object_context: list[dict] | None = None,
) -> dict:
    """Per-object fit. Text in `substituted` fonts (rendered here with another font) is not
    held to line-width tolerance; it is marked width_unverified instead."""
    page_h, page_w = source.shape[:2]
    context = object_context if object_context is not None else objects
    fits = [measure_object(obj, source, rendered, page_w, page_h, substituted, default_font, context) for obj in objects]
    measured = [f for f in fits if f["status"] in ("ok", "off", "missing_in_render", "extra_in_render")]
    ok = sum(1 for f in measured if f["status"] == "ok")
    worst = sorted((f for f in measured if f["status"] != "ok"), key=object_error, reverse=True)
    return {
        "tolerance_px": round(position_tolerance(page_w), 2),
        "measured": len(measured),
        "within_tolerance": ok,
        "fit_ratio": round(ok / len(measured), 4) if measured else 1.0,
        "worst": [f["id"] for f in worst[:10]],
        "width_unverified": [f["id"] for f in fits if f.get("width_unverified")],
        "font_hints": [
            {"id": f["id"], "weight_ratio": f.get("weight_ratio"), "hint": f["font_hint"]} for f in fits if f.get("font_hint")
        ],
        "objects": fits,
    }


# ---------------------------------------------------------------- refinement


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _clamp_box(box: list[float], page_w: float, page_h: float) -> list[float]:
    x, y, w, h = box
    w = max(1.0, min(w, page_w))
    h = max(1.0, min(h, page_h))
    x = max(0.0, min(x, page_w - w))
    y = max(0.0, min(y, page_h - h))
    return [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]


def _snap_font_size(base: float, factor: float) -> float:
    """Half-point size nearest the request, but a sub-point request moves only one step.

    Rounding 12.5pt * 0.933 to the nearest half point lands on 11.5 and overshoots
    a one-pixel glyph. The next render is then worse, and the loop rolls it back.
    """
    target = base * factor
    delta = target - base
    if abs(delta) < 1.0:
        new = base if abs(delta) < 0.25 else base + (0.5 if delta > 0 else -0.5)
    else:
        new = round(target * 2) / 2
    return max(4.0, new)


def _scale_fonts(obj: dict, factor: float) -> tuple[bool, bool]:
    style = obj.setdefault("style", {})
    base = float(style.get("font_size_pt", 18))
    requested = factor
    factor = min(max(factor, FONT_SCALE_LIMITS[0]), FONT_SCALE_LIMITS[1])
    clamped = factor != requested
    new = _snap_font_size(base, factor)
    if new == base:
        return False, clamped
    style["font_size_pt"] = new
    for run in obj.get("runs") or []:
        if "font_size_pt" in run:
            run["font_size_pt"] = _snap_font_size(float(run["font_size_pt"]), factor)
    return True, clamped


def _remove_cjk_latin_spaces(obj: dict, spans: list[tuple[int, int]] | None = None) -> bool:
    boundary = r"\u3400-\u9fff\uf900-\ufaffA-Za-z0-9"
    punctuation = r"，。；：！？、（）【】《》「」『』“”‘’％·/…—～"
    pattern_a = re.compile(rf"(?<=[{boundary}])\s+(?=[{boundary}{punctuation}])")
    pattern_b = re.compile(rf"(?<=[{boundary}{punctuation}])\s+(?=[{boundary}])")

    def clean(text: str) -> str:
        return pattern_b.sub("", pattern_a.sub("", text))

    allowed = None if spans is None else {index for start, end in spans for index in range(start, end)}
    changed = False
    if isinstance(obj.get("text"), str):
        new = "".join(
            ch for index, ch in enumerate(obj["text"])
            if not (ch.isspace() and (allowed is None or index in allowed))
        ) if allowed is not None else clean(obj["text"])
        changed = new != obj["text"]
        obj["text"] = new
    runs = obj.get("runs") or []
    offset = 0
    for run in runs:
        if isinstance(run, dict) and isinstance(run.get("text"), str):
            raw = run["text"]
            new = "".join(
                ch for index, ch in enumerate(raw, start=offset)
                if not (ch.isspace() and (allowed is None or index in allowed))
            ) if allowed is not None else clean(raw)
            changed |= new != run["text"]
            run["text"] = new
            offset += len(raw)
    for left, right in zip(runs, runs[1:]):
        if allowed is not None:
            continue
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        lt = str(left.get("text", ""))
        rt = str(right.get("text", ""))
        combined = lt + rt
        cleaned = clean(combined)
        if cleaned != combined:
            if lt.endswith(" "):
                left["text"] = lt.rstrip(" ")
            if rt.startswith(" "):
                right["text"] = rt.lstrip(" ")
            changed = True
    return changed


def _shape_text(obj: dict, fit: dict, page_w: float, page_h: float, tune_width: bool = True,
                baseline: float | None = None) -> bool:
    """Size, wrapping, width and spacing: one correction per round, in dependency order."""
    style = obj.setdefault("style", {})
    x, y, w, h = (float(v) for v in obj["bbox"])
    tol = position_tolerance(page_w)
    text = text_of(obj)

    # 1. Font size from the first line's glyph height.
    scale_h = fit["scale_h"]
    if fit.get("suspect") == "measurement":
        return False
    if abs(scale_h - 1) > SIZE_TOLERANCE:
        base = float(style.get("font_size_pt", 18))
        capped = min(max(scale_h, FONT_SCALE_LIMITS[0]), FONT_SCALE_LIMITS[1])
        target = base * capped
        if not _within_cumulative(target, baseline):
            fit["suspect"] = "cumulative_font_cap"
            return False
        changed, clamped = _scale_fonts(obj, scale_h)
        if clamped:
            fit["clamped"] = True
        if changed:
            return True

    # Whitespace is a second-pass correction.  In particular, never remove a
    # space in the same round that changes font size.
    if (
        fit.get("spacing_remove")
        and abs(scale_h - 1) <= 2 * SIZE_TOLERANCE
        and _remove_cjk_latin_spaces(obj, fit.get("spacing_spans"))
    ):
        fit["spacing_removed"] = True
        return True

    # 1b. Stroke weight.
    if fit.get("weight_fix") == "bold":
        style["bold"] = True
        return True
    if fit.get("weight_fix") == "regular":
        style["bold"] = False
        return True

    # 2. Line count (wrapping).
    src_n, ren_n = fit["source_lines"], fit["render_lines"]
    if ren_n > src_n:
        if src_n == 1:
            style["wrap"] = False
            w = max(w, fit["source_width"] * 1.1)
        else:
            w *= 1.06
        obj["bbox"] = _clamp_box([x, y, w, h], page_w, page_h)
        return True
    if ren_n < src_n and "\n" not in text and style.get("wrap", True) is not False:
        obj["bbox"] = _clamp_box([x, y, w * 0.95, h], page_w, page_h)
        return True

    # 3. First-line width: fine font size first (glyph height cannot resolve
    #    half a point, width can), then letter spacing for substituted fonts.
    src_w, ren_w = fit["source_width"], fit["render_width"]
    n_chars = len(text.split("\n")[0])
    if tune_width and fit.get("width_comparable") and n_chars > 1 and abs(src_w - ren_w) > max(2 * tol, WIDTH_TOLERANCE * src_w):
        if (
            src_w < ren_w
            and abs(scale_h - 1) <= 2 * SIZE_TOLERANCE
            and _remove_cjk_latin_spaces(obj, fit.get("spacing_spans"))
        ):
            fit["spacing_removed"] = True
            return True
        scale_w = src_w / max(ren_w, 1)
        if "char_spacing_pt" not in style and abs(scale_w - 1) <= FONT_FINE_TUNE_LIMIT:
            base = float(style.get("font_size_pt", 18))
            capped = min(max(scale_w, FONT_SCALE_LIMITS[0]), FONT_SCALE_LIMITS[1])
            target = base * capped
            if not _within_cumulative(target, baseline):
                fit["suspect"] = "cumulative_font_cap"
                return False
            changed, clamped = _scale_fonts(obj, scale_w)
            if clamped:
                fit["clamped"] = True
            if changed:
                return True
        delta_pt = (src_w - ren_w) * pt_per_px(page_w, page_h) / (n_chars - 1)
        spacing = float(style.get("char_spacing_pt", 0)) + delta_pt
        cap = 0.12 * float(style.get("font_size_pt", 18))
        style["char_spacing_pt"] = round(min(max(spacing, -cap), cap), 2)
        return True

    # 4. Line spacing from the line pitch.
    pitch = fit.get("pitch_ratio")
    if pitch is not None and abs(pitch - 1) > PITCH_TOLERANCE:
        current = float(style.get("line_spacing_multiple", 1.0))
        style["line_spacing_multiple"] = round(min(max(current * pitch, LINE_SPACING_LIMITS[0]), LINE_SPACING_LIMITS[1]), 3)
        return True
    return False


def refine_text(obj: dict, fit: dict, page_w: float, page_h: float, tune_width: bool = True,
                baseline: float | None = None, position_only: bool = False) -> bool:
    """tune_width=False when the render environment substitutes this object's font: line
    width then reflects the substitute's metrics, and matching it would make the text
    wrong in PowerPoint with the real font.

    position_only skips font size, weight, wrapping and spacing for one round after
    a font-size edit was rolled back.
    """
    if fit.get("suspect") == "measurement":
        return False
    changed = False if position_only else _shape_text(obj, fit, page_w, page_h, tune_width, baseline)
    # Position every round: moving the box does not disturb size or wrapping,
    # and the next measurement absorbs any drift a size change causes.
    x, y, w, h = (float(v) for v in obj["bbox"])
    dx, dy = fit.get("anchor_dx", 0.0), fit.get("anchor_dy", 0.0)
    if abs(dx) > position_tolerance(page_w) / 2 or abs(dy) > position_tolerance(page_w) / 2:
        limit = max(8.0, 0.5 * h)
        new = _clamp_box([x + _clamp(dx, limit), y + _clamp(dy, limit), w, h], page_w, page_h)
        if new != obj["bbox"]:
            obj["bbox"] = new
            changed = True
    return changed


def refine_box(obj: dict, fit: dict, page_w: float, page_h: float, step_scale: float = 1.0) -> bool:
    x, y, w, h = (float(v) for v in obj["bbox"])
    limit_x = max(8.0, 0.25 * w)
    limit_y = max(8.0, 0.25 * h)
    scale = step_scale if step_scale > 0 else 1.0
    x0 = x + _clamp(fit["dx0"], limit_x) * scale
    x1 = x + w + _clamp(fit["dx1"], limit_x) * scale
    y0 = y + _clamp(fit["dy0"], limit_y) * scale
    y1 = y + h + _clamp(fit["dy1"], limit_y) * scale
    if x1 - x0 < 1 or y1 - y0 < 1:
        return False
    new = _clamp_box([x0, y0, x1 - x0, y1 - y0], page_w, page_h)
    if new == _clamp_box([x, y, w, h], page_w, page_h):
        return False
    obj["bbox"] = new
    return True


def _update_head_size(obj: dict, fit: dict, page_w: float) -> bool:
    """Grow a custom arrow head when the source ink is thicker than the render.

    Only horizontal and vertical arrows. Width targets the source thickness and
    moves at most 30% per round; length starts at 0.9 times the width.
    """
    style = obj.setdefault("style", {})
    if style.get("end_arrow") in (None, "none") and style.get("start_arrow") in (None, "none"):
        return False
    x1, y1, x2, y2 = (float(v) for v in obj["points"])
    horizontal = abs(y1 - y2) < 1 and abs(x1 - x2) >= 1
    vertical = abs(x1 - x2) < 1 and abs(y1 - y2) >= 1
    if not horizontal and not vertical:
        return False
    src, ren = fit.get("source_ink"), fit.get("render_ink")
    if not src or not ren or len(src) != 4 or len(ren) != 4:
        return False
    src_t = (src[3] - src[1]) if horizontal else (src[2] - src[0])
    ren_t = (ren[3] - ren[1]) if horizontal else (ren[2] - ren[0])
    if ren_t <= 0 or src_t <= ren_t + position_tolerance(page_w):
        return False
    current = style.get("head_size_px")
    if (
        isinstance(current, (list, tuple))
        and len(current) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in current)
        and float(current[0]) > 0
        and float(current[1]) > 0
    ):
        cw, cl = float(current[0]), float(current[1])
    else:
        cw, cl = float(ren_t), float(ren_t) * 0.9
    delta_w = max(-0.3 * cw, min(0.3 * cw, float(src_t) - cw))
    nw = cw + delta_w
    delta_l = max(-0.3 * cl, min(0.3 * cl, nw * 0.9 - cl))
    nl = cl + delta_l
    new_pair = [round(nw, 1), round(nl, 1)]
    if new_pair[0] <= 0 or new_pair[1] <= 0 or new_pair == [round(cw, 1), round(cl, 1)]:
        return False
    style["head_size_px"] = new_pair
    return True


def refine_line(obj: dict, fit: dict, page_w: float, page_h: float, step_scale: float = 1.0) -> bool:
    x1, y1, x2, y2 = (float(v) for v in obj["points"])
    # Per-round move limit scales with the line, as for boxes, so long lines converge quickly.
    limit = max(16.0, 0.25 * max(abs(x2 - x1), abs(y2 - y1)))
    scale = step_scale if step_scale > 0 else 1.0

    def move(a: float, b: float, d0: float, d1: float) -> tuple[float, float]:
        if abs(a - b) < 1:  # degenerate axis (horizontal/vertical line): move by the centre shift
            d = (d0 + d1) / 2
            return a + _clamp(d, limit), b + _clamp(d, limit)
        lo, hi = (d0, d1) if a < b else (d1, d0)
        return a + _clamp(lo, limit), b + _clamp(hi, limit)

    nx1, nx2 = move(x1, x2, fit["dx0"] * scale, fit["dx1"] * scale)
    ny1, ny2 = move(y1, y2, fit["dy0"] * scale, fit["dy1"] * scale)

    def clamp(v: float, hi: float) -> float:
        return round(max(0.0, min(hi, v)), 1)

    changed = False
    new = [clamp(nx1, page_w), clamp(ny1, page_h), clamp(nx2, page_w), clamp(ny2, page_h)]
    if not ((new[0] == new[2] and new[1] == new[3]) or new == [round(v, 1) for v in (x1, y1, x2, y2)]):
        obj["points"] = new
        changed = True
    if _update_head_size(obj, fit, page_w):
        changed = True
    style = obj.setdefault("style", {})
    # Stroke width is measured independently on the shaft.  Correct only when
    # the discrepancy is real, and cap each round at ±40%.
    src_width = fit.get("stroke_width_source_px")
    ren_width = fit.get("stroke_width_render_px")
    if isinstance(src_width, (int, float)) and isinstance(ren_width, (int, float)) and ren_width > 0:
        if abs(src_width - ren_width) > 1.0:
            ratio = float(src_width) / float(ren_width)
            ratio = min(1.4, max(0.6, ratio))
            old = float(style.get("width_pt", 1.0))
            new = round(old * ratio, 2)
            if new > 0 and new != old:
                style["width_pt"] = new
                changed = True
    # Source head classification is authoritative.  A mismatch is actionable
    # even when the current preset is already lg; use a custom head size when
    # the measured head exceeds the preset range.
    source_kind = fit.get("head_kind_source")
    current_end = style.get("end_arrow", "none")
    current_start = style.get("start_arrow", "none")
    if source_kind in {"open", "triangle"}:
        field = "end_arrow" if current_end not in (None, "none") else "start_arrow"
        if style.get(field) != source_kind:
            style[field] = source_kind
            changed = True
    hw = fit.get("head_w_source_px")
    hl = fit.get("head_len_source_px")
    if isinstance(hw, (int, float)) and isinstance(hl, (int, float)) and hw > 0 and hl > 0:
        stroke = float(src_width or 0.0)
        if stroke > 0 and hw > 6.0 * stroke:
            pair = [round(float(hw), 1), round(float(hl), 1)]
            if style.get("head_size_px") != pair:
                style["head_size_px"] = pair
                changed = True
    # A custom triangle replaces DrawingML headEnd, so preset sizes no longer apply.
    if style.get("head_size_px"):
        return changed
    order = ("sm", "med", "lg")
    for field in ("head_w", "head_len"):
        current = (obj.get("style") or {}).get(field, "med")
        source = fit.get(f"{field}_source")
        render = fit.get(f"{field}_render")
        if source in order and render in order and source != render:
            cur_idx = order.index(current) if current in order else 1
            src_idx = order.index(source)
            ren_idx = order.index(render)
            step = 1 if src_idx > ren_idx else -1
            new_val = order[max(0, min(2, cur_idx + step))]
            if new_val != (obj.get("style") or {}).get(field):
                obj.setdefault("style", {})[field] = new_val
                changed = True
    return changed


def uses_font(obj: dict, fonts: set[str], default: str = "Arial") -> bool:
    names = {(obj.get("style") or {}).get("font_face") or default}
    names |= {r["font_face"] for r in obj.get("runs") or [] if r.get("font_face")}
    return bool({n.lower() for n in names} & {f.lower() for f in fonts})


def refine_page(page: dict, fits: list[dict], substituted_fonts: set[str] | None = None,
                default_font: str = "Arial", baseline_state: dict | None = None,
                frozen: set[str] | None = None, limits: dict | None = None,
                skip: set[str] | None = None) -> list[str]:
    """Apply measured corrections to one PageIR page in place. Returns changed object ids.

    `limits` and `skip` are loop memory (full id or object id), never fields on the object.
    `position_only` blocks font-size edits; `step_scale` shrinks a shape or line step.
    """
    substituted_fonts = substituted_fonts or set()
    limits = limits or {}
    skip = skip or set()
    frozen = frozen or set()
    page_w = float(page["width_px"])
    page_h = float(page["height_px"])
    by_id = {str(f.get("id")): f for f in fits}
    changed = []
    drop = []
    for obj in page.get("objects", []):
        oid = str(obj.get("id"))
        full_id = f"{page.get('id')}:{oid}"
        fit = by_id.get(oid)
        if not fit or oid in frozen or full_id in frozen or oid in skip or full_id in skip:
            continue
        if obj.get("type") == "line" and fit.get("status") in ("extra_in_render", "missing_in_render", "no_ink"):
            pts = obj.get("points")
            if isinstance(pts, list) and len(pts) == 4:
                length = ((float(pts[2]) - float(pts[0])) ** 2 + (float(pts[3]) - float(pts[1])) ** 2) ** 0.5
                if length <= 40:
                    drop.append(obj)
                    changed.append(oid)
                    continue
        if fit.get("status") != "off":
            continue
        limit = limits.get(full_id) or limits.get(oid) or {}
        if obj.get("type") == "text":
            baseline = (baseline_state or {}).get(str(obj.get("id")))
            did = refine_text(
                obj, fit, page_w, page_h, not uses_font(obj, substituted_fonts, default_font), baseline,
                position_only=bool(limit.get("position_only")),
            )
        elif obj.get("type") == "line":
            did = refine_line(obj, fit, page_w, page_h, step_scale=float(limit.get("step_scale", 1.0)))
        else:
            did = refine_box(obj, fit, page_w, page_h, step_scale=float(limit.get("step_scale", 1.0)))
        if did:
            changed.append(str(obj.get("id")))
    for obj in drop:
        page["objects"].remove(obj)
    return changed


def substituted_fonts(report: dict) -> set[str]:
    """Fonts that render_compare found substituted in the render environment."""
    return {
        s["requested"]
        for s in (report.get("fonts") or {}).get("substitutions", [])
        if s.get("substituted") and not s.get("metric_compatible")
    }


def refine_payload(payload: dict, slide_reports: list[dict], substituted: set[str] | None = None,
                   baseline_state: dict | None = None, frozen: set[str] | None = None,
                   limits: dict | None = None, skip: set[str] | None = None) -> tuple[dict, list[str]]:
    refined = copy.deepcopy(payload)
    default_font = payload.get("default_font_face", "Arial")
    changed: list[str] = []
    for index, report in enumerate(slide_reports):
        if index >= len(refined.get("pages", [])):
            break
        fits = (report.get("object_fit") or {}).get("objects", [])
        page = refined["pages"][index]
        changed += [
            f"{page.get('id')}:{oid}"
            for oid in refine_page(page, fits, substituted, default_font, baseline_state, frozen, limits, skip)
        ]
    return refined, changed
