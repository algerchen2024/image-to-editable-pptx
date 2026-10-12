#!/usr/bin/env python3
"""Measure flat-colored panels, outlined boxes, circles, and rules in a source image.

Produces PageIR shape/line objects with measured geometry, colors, stroke
widths and corner radii, so the model does not have to estimate them by eye.

Examples:
    detect_shapes.py source.png --out analysis/shapes.json
    detect_shapes.py source.png --merge page_ir.json --out page_ir.json   # add to a draft

With --merge, components lying inside existing text boxes are ignored (glyph
strokes such as "一" are not rules) and detected objects are appended to the
page. Review the result: gradients, shadows, dashed borders, and icons are not
detected and must be authored or kept as image assets.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_ops import border_color, dilate, ink_box, label_components  # noqa: E402
from pageir_common import pt_per_px, rgb_to_hex  # noqa: E402


COLOR_TOLERANCE = 14  # flat fills render uniformly
STROKE_TOLERANCE = 44  # thin strokes are anti-aliased across several shades
STROKE_MIN_CONTRAST = 24  # light-gray card borders are only ~30 levels darker than white
MIN_COLOR_SHARE = 0.0008
MIN_STROKE_SHARE = 0.0001  # a 1 px rule is only a few hundred pixels
MAX_COLORS = 24
MIN_PANEL = 20
def classify_arrow_head(ratio: float) -> str:
    """Map measured head width/stroke width to DrawingML's sm/med/lg."""
    if ratio < 3:
        return "sm"
    if ratio < 5:
        return "med"
    return "lg"


HEAD_SIZE_RATIO = 6.0


def rule_arrow_decision(rule: dict) -> dict:
    """Preset DrawingML head, measured triangle, or image when direction is unknown.

    A head wider than 6 stroke widths is ``head_size_px`` (native triangle).
    Cropping to an image is only the fallback when the line has no direction.
    """
    arrow = rule.get("arrow") or {}
    if not arrow:
        return {"mode": "none"}
    ratio = float(arrow.get("ratio") or 0)
    arrow_kind = arrow.get("kind", "triangle")
    if ratio <= HEAD_SIZE_RATIO:
        size = arrow.get("size") or classify_arrow_head(ratio)
        return {"mode": "preset", "arrow": arrow_kind, "head_w": size, "head_len": size}
    points = rule.get("points") or []
    if len(points) != 4:
        return {"mode": "image"}
    x1, y1, x2, y2 = (float(v) for v in points)
    if math.hypot(x2 - x1, y2 - y1) < 8:
        return {"mode": "image"}
    return {
        "mode": "head_size",
        "arrow": arrow_kind,
        "head_size_px": [round(float(arrow["head_w_px"]), 1), round(float(arrow["head_len_px"]), 1)],
    }


def measure_arrow_head(image: np.ndarray, points: list[float], color: str, stroke_px: float) -> dict | None:
    """Measure a triangular arrow head around the line's second endpoint.

    The result is intentionally conservative: a head is reported only when
    the endpoint has a connected ink cluster wider than the shaft.
    """
    if not isinstance(points, list) or len(points) != 4 or stroke_px <= 0:
        return None
    x1, y1, x2, y2 = (float(v) for v in points)
    vx, vy = x2 - x1, y2 - y1
    length = math.hypot(vx, vy)
    if length < 8:
        return None
    ux, uy = vx / length, vy / length
    px, py = -uy, ux
    h, w = image.shape[:2]
    radius = int(max(24, min(120, round(20 * stroke_px))))
    x0, x1i = max(0, int(round(x2)) - radius), min(w, int(round(x2)) + radius + 1)
    y0, y1i = max(0, int(round(y2)) - radius), min(h, int(round(y2)) + radius + 1)
    if x1i <= x0 or y1i <= y0:
        return None
    from pageir_common import normalize_hex

    digits = normalize_hex(color)[1:]
    target = np.array([int(digits[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.int32)
    crop = image[y0:y1i, x0:x1i, :3].astype(np.int32)
    mask = np.abs(crop - target).max(axis=2) <= 48
    ys, xs = np.nonzero(mask)
    if len(xs) < max(8, int(stroke_px * 3)):
        return None
    # Only the ink connected to the tip. A same-colored icon inside the search
    # radius (the bookmark above arrow-3) is not part of the head.
    tip_x = float(x2) - x0
    tip_y = float(y2) - y0
    dist2 = (xs.astype(np.float64) - tip_x) ** 2 + (ys.astype(np.float64) - tip_y) ** 2
    seed_i = int(np.argmin(dist2))
    reach = max(12.0, 3.0 * float(stroke_px))
    if float(dist2[seed_i]) > reach * reach:
        return None
    sy, sx = int(ys[seed_i]), int(xs[seed_i])
    keep = np.zeros(mask.shape, dtype=bool)
    stack = [(sy, sx)]
    keep[sy, sx] = True
    mh, mw = mask.shape
    while stack:
        cy, cx = stack.pop()
        for dy in (-1, 0, 1):
            ny = cy + dy
            if ny < 0 or ny >= mh:
                continue
            for dx in (-1, 0, 1):
                nx = cx + dx
                if nx < 0 or nx >= mw or keep[ny, nx] or not mask[ny, nx]:
                    continue
                keep[ny, nx] = True
                stack.append((ny, nx))
    mask = keep
    ys, xs = np.nonzero(mask)
    if len(xs) < max(8, int(stroke_px * 3)):
        return None
    gx, gy = xs + x0 - x2, ys + y0 - y2
    longitudinal = gx * ux + gy * uy
    lat_signed = gx * px + gy * py
    lateral = np.abs(lat_signed)
    near = (longitudinal <= radius) & (longitudinal >= -radius)
    if not near.any():
        return None
    wing_thresh = max(1.5, stroke_px * 0.7)
    wings = (lateral >= wing_thresh) & near
    if not wings.any():
        return None
    if not ((lat_signed[wings] > wing_thresh).any() and (lat_signed[wings] < -wing_thresh).any()):
        return None
    head_width = float(lat_signed[wings].max() - lat_signed[wings].min() + 1)
    neg_span = max(0.0, -float(np.percentile(longitudinal[wings], 2)))
    pos_span = max(0.0, float(np.percentile(longitudinal[wings], 98)))
    forward = -1.0 if neg_span >= pos_span else 1.0
    if forward < 0:
        base_long = float(longitudinal[wings].min())
        tip_long = float(longitudinal.max())
    else:
        base_long = float(longitudinal[wings].max())
        tip_long = float(longitudinal.min())
    head_len = max(abs(tip_long - base_long) + 1.0, stroke_px * 1.2)
    ratio = head_width / max(float(stroke_px), 1.0)
    if ratio < 1.6 or head_len < 1.2 * stroke_px:
        return None
    tip_wx = float(x2 + tip_long * ux)
    tip_wy = float(y2 + tip_long * uy)
    gy_grid, gx_grid = np.mgrid[y0:y1i, x0:x1i]
    cx_grid = gx_grid.astype(np.float64) - tip_wx
    cy_grid = gy_grid.astype(np.float64) - tip_wy
    long_grid = cx_grid * ux + cy_grid * uy
    across_grid = cx_grid * px + cy_grid * py
    depth_grid = -long_grid if forward < 0 else long_grid
    triangle = (depth_grid >= -0.5) & (depth_grid <= head_len + 0.5) & (
        np.abs(across_grid) <= (np.maximum(0.0, depth_grid) / head_len) * (head_width / 2.0) + 0.5
    )
    head_region = triangle & (np.abs(across_grid) >= stroke_px * 0.6)
    tri_area = float(head_region.sum())
    ink_in_tri = float((mask & head_region).sum())
    fill_ratio = (ink_in_tri / tri_area) if tri_area > 0 else 1.0
    kind = "open" if fill_ratio < 0.45 else "triangle"
    return {
        "head_w_px": round(head_width, 2),
        "head_len_px": round(head_len, 2),
        "ratio": round(ratio, 3),
        "size": classify_arrow_head(ratio),
        "kind": kind,
        "fill_ratio": round(fill_ratio, 3),
    }


def candidate_colors(
    image: np.ndarray,
    background: np.ndarray,
    tolerance: int = COLOR_TOLERANCE,
    min_contrast: int = COLOR_TOLERANCE,
    min_share: float = MIN_COLOR_SHARE,
    keep: np.ndarray | None = None,
) -> list[np.ndarray]:
    """Frequent colors that differ from the page background, deduplicated within `tolerance`.

    `keep` limits the count to pixels outside text boxes and image assets, so
    anti-aliased glyph shades do not use up the color slots.
    """
    pixels = image.reshape(-1, 3).astype(np.int32)
    if keep is not None:
        pixels = pixels[keep.reshape(-1)]
    keys = (pixels // 8) @ np.array([1024, 32, 1])
    values, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    order = np.argsort(-counts)
    total = pixels.shape[0]
    colors: list[np.ndarray] = []
    for idx in order:
        if counts[idx] < min_share * total or len(colors) >= MAX_COLORS:
            break
        color = pixels[inverse.reshape(-1) == idx].mean(axis=0)
        if np.abs(color - background).max() <= min_contrast:
            continue
        # Deduplicate relative to contrast: an axis gray (#AAA) is not the bar gray (#CCC).
        if any(np.abs(color - c).max() <= min(tolerance, max(6, 0.5 * np.abs(c - background).max())) for c in colors):
            continue
        colors.append(color)
    return colors


def edge_coverage(mask: np.ndarray, band: int = 3) -> list[float]:
    """Coverage of the middle 60% of the top, bottom, left and right edges.

    Each edge is the union of its outer `band` rows/columns, so an anti-aliased
    outermost pixel row does not hide a border.
    """
    h, w = mask.shape
    b = max(1, min(band, h // 4, w // 4))
    xs = slice(int(w * 0.2), max(int(w * 0.8), int(w * 0.2) + 1))
    ys = slice(int(h * 0.2), max(int(h * 0.8), int(h * 0.2) + 1))
    return [
        float(mask[:b, xs].any(axis=0).mean()),
        float(mask[-b:, xs].any(axis=0).mean()),
        float(mask[ys, :b].any(axis=1).mean()),
        float(mask[ys, -b:].any(axis=1).mean()),
    ]


def corner_radius(mask: np.ndarray) -> float:
    """Average corner radius from how far the diagonal stays empty at each corner."""
    h, w = mask.shape
    limit = min(h, w) // 2
    radii = []
    for flip_y, flip_x in ((False, False), (False, True), (True, False), (True, True)):
        m = mask[::-1] if flip_y else mask
        m = m[:, ::-1] if flip_x else m
        d = 0
        while d < limit and not m[d, d]:
            d += 1
        radii.append(d / (1 - 1 / math.sqrt(2)) if d else 0.0)
    return float(np.median(radii))


def stroke_width(mask: np.ndarray) -> int:
    """Median run length of mask pixels entering from each edge at the middle."""
    h, w = mask.shape
    runs = []
    for line in (mask[h // 2, :], mask[h // 2, ::-1], mask[:, w // 2], mask[::-1, w // 2]):
        n = 0
        while n < len(line) and line[n]:
            n += 1
        if n:
            runs.append(n)
    return int(np.median(runs)) if runs else 1


def inside_any(box, regions, share: float = 0.6) -> bool:
    x, y, w, h = box
    area = max(w * h, 1)
    for rx, ry, rw, rh in regions:
        ix = max(0, min(x + w, rx + rw) - max(x, rx))
        iy = max(0, min(y + h, ry + rh) - max(y, ry))
        if ix * iy >= share * area:
            return True
    return False


def _rule_axis(rule: dict) -> tuple[str, float, float, float]:
    """(orientation, perpendicular position, start, end)."""
    x1, y1, x2, y2 = rule["points"]
    if y1 == y2:
        return "h", y1, min(x1, x2), max(x1, x2)
    return "v", x1, min(y1, y2), max(y1, y2)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0)) / max(min(a1 - a0, b1 - b0), 1e-6)


def _hex_rgb(value: str) -> np.ndarray:
    return np.array([int(value[i : i + 2], 16) for i in (1, 3, 5)], dtype=np.float64)


def is_blend(color: str, a: np.ndarray, b: np.ndarray, residual: float = 24.0) -> bool:
    """True when `color` lies (within `residual`) on the segment between colors a and b."""
    c = _hex_rgb(color)
    d = b - a
    denom = float(d @ d)
    t = 0.0 if denom == 0 else float(np.clip((c - a) @ d / denom, 0.0, 1.0))
    return float(np.abs(c - (a + t * d)).max()) <= residual


def clean_rules(
    rules: list[dict], shapes: list[tuple[list[float], list[str]]], background: np.ndarray, near: float = 4.0
) -> list[dict]:
    """Drop anti-aliasing halos along detected shapes and merge split rules.

    A shape edge renders with 1 px blended shades that show up as thin "rules".
    A rule hugging a shape edge is dropped only when its color is a blend of
    that shape's color and the background, so real separators on a fill edge
    (table grid lines on zebra rows) survive. Parallel rules within `near` px
    of each other are one rule: keep the most contrasting color and use the
    combined thickness.
    """
    kept = []
    for rule in rules:
        axis, pos, start, end = _rule_axis(rule)
        halo = False
        for (x, y, w, h), colors in shapes:
            if not any(is_blend(rule["color"], background, _hex_rgb(c)) for c in colors if c):
                continue
            if axis == "h":
                edges, span = (y, y + h), (x, x + w)
            else:
                edges, span = (x, x + w), (y, y + h)
            if any(abs(pos - e) <= near for e in edges) and _overlap(start, end, *span) >= 0.5:
                halo = True
                break
        if not halo:
            kept.append(rule)

    def contrast(rule: dict) -> float:
        rgb = np.array([int(rule["color"][i : i + 2], 16) for i in (1, 3, 5)])
        return float(np.abs(rgb - background).max())

    groups: list[list[dict]] = []
    for rule in sorted(kept, key=lambda r: _rule_axis(r)[1]):
        axis, pos, start, end = _rule_axis(rule)
        for group in groups:
            g_axis, g_pos, g_start, g_end = _rule_axis(group[-1])
            if g_axis == axis and abs(pos - g_pos) <= near and _overlap(start, end, g_start, g_end) >= 0.5:
                group.append(rule)
                break
        else:
            groups.append([rule])

    merged = []
    for group in groups:
        # Shape-measured hairlines carry their true color; otherwise take the most contrasting shade.
        measured = [r for r in group if r.get("measured")]
        best = max(measured or group, key=lambda r: (r["thickness"], contrast(r)) if measured else contrast(r))
        if len(group) > 1:
            axis = _rule_axis(best)[0]
            lows = [_rule_axis(r)[1] - r["thickness"] / 2 for r in group]
            highs = [_rule_axis(r)[1] + r["thickness"] / 2 for r in group]
            centre = (min(lows) + max(highs)) / 2
            x1, y1, x2, y2 = best["points"]
            best = {
                **best,
                "points": [x1, centre, x2, centre] if axis == "h" else [centre, y1, centre, y2],
                "thickness": max(highs) - min(lows),
            }
        merged.append(best)
    return merged


def color_tolerance(color: np.ndarray, background: np.ndarray, limit: int) -> int:
    """Match tolerance for a color: at most half its contrast, so a light-gray border never matches the page."""
    contrast = float(np.abs(color - background).max())
    return int(max(6, min(limit, 0.5 * contrast)))


def is_halo(rule: dict, img: np.ndarray, strong: list[np.ndarray], background: np.ndarray) -> bool:
    """A thin rule that is a blend toward a stronger color running right beside it is anti-aliasing."""
    blends = [c for c in strong if is_blend(rule["color"], background, c) and rgb_to_hex(c) != rule["color"]]
    if not blends:
        return False
    axis, pos, start, end = _rule_axis(rule)
    h, w = img.shape[:2]
    samples = np.linspace(start, end, num=max(8, int((end - start) // 6)))
    hits = 0
    for s in samples:
        if axis == "h":
            ys, xs = slice(max(0, int(pos) - 3), min(h, int(pos) + 4)), slice(int(s), int(s) + 1)
        else:
            ys, xs = slice(int(s), int(s) + 1), slice(max(0, int(pos) - 3), min(w, int(pos) + 4))
        patch = img[ys, xs].reshape(-1, 3)
        for c in blends:
            # The line's own pixels must not count as the stronger color.
            tol = min(24.0, 0.5 * float(np.abs(_hex_rgb(rule["color"]) - c).max()))
            if (np.abs(patch - c).max(axis=1) <= tol).any():
                hits += 1
                break
    return hits >= 0.5 * len(samples)


THIN_CONTRAST = 10
THIN_GAP = 3


def _runs(row: np.ndarray, min_len: int, max_gap: int = 2) -> list[tuple[int, int]]:
    """[start, end) runs of True in a 1-D mask, bridging gaps of up to max_gap."""
    idx = np.flatnonzero(row)
    if idx.size == 0:
        return []
    runs = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > max_gap + 1:
            if prev + 1 - start >= min_len:
                runs.append((start, prev + 1))
            start = i
        prev = i
    if prev + 1 - start >= min_len:
        runs.append((start, prev + 1))
    return runs


def thin_lines(img: np.ndarray, background: np.ndarray, keep: np.ndarray, min_len: int) -> list[dict]:
    """Hairlines (1-4 px) found by shape, not color: ink with background 3 px to both sides.

    Anti-aliasing spreads a thin border over several shades, so exact color
    matching breaks it apart; this does not. The color is the mean of the
    darkest pixel across the line at each position.
    """
    diff = np.abs(img - background).max(axis=2)
    ink = (diff > THIN_CONTRAST) & keep
    quiet = diff <= THIN_CONTRAST
    found = []
    for axis in ("h", "v"):
        d = diff if axis == "h" else diff.T
        m = ink if axis == "h" else ink.T
        q = quiet if axis == "h" else quiet.T
        image = img if axis == "h" else img.transpose(1, 0, 2)
        thin = m.copy()
        thin[THIN_GAP:-THIN_GAP] &= q[: -2 * THIN_GAP] | q[2 * THIN_GAP :]
        # Background must show on *both* sides somewhere within 3 px.
        above = np.zeros_like(m)
        below = np.zeros_like(m)
        for k in range(1, THIN_GAP + 2):
            above[k:] |= q[:-k]
            below[:-k] |= q[k:]
        thin = m & above & below
        segments = []
        for y in range(thin.shape[0]):
            for x0, x1 in _runs(thin[y], min_len):
                segments.append([y, x0, x1])
        # Merge adjacent rows of the same line into one rule with a thickness.
        segments.sort()
        merged: list[list[int]] = []
        for y, x0, x1 in segments:
            for line in merged:
                if y - line[1] <= 1 and min(x1, line[3]) - max(x0, line[2]) >= 0.5 * min(x1 - x0, line[3] - line[2]):
                    line[1] = y
                    line[2], line[3] = min(line[2], x0), max(line[3], x1)
                    break
            else:
                merged.append([y, y, x0, x1])
        for y0, y1, x0, x1 in merged:
            if y1 - y0 + 1 > 4:
                continue
            band = d[y0 : y1 + 1, x0:x1]
            rows = np.argmax(band, axis=0) + y0
            colors = image[rows, np.arange(x0, x1)]
            color = rgb_to_hex(colors.mean(axis=0))
            centre = (y0 + y1 + 1) / 2
            points = [x0, centre, x1, centre] if axis == "h" else [centre, x0, centre, x1]
            found.append(
                {"points": [float(v) for v in points], "color": color, "thickness": float(y1 - y0 + 1), "measured": True}
            )
    return found


def join_collinear(rules: list[dict], panels: list[dict], slack: float = 3.0) -> list[dict]:
    """Join collinear rule segments whose gap is covered by a panel resting on the line (bars on an axis)."""
    out = sorted(rules, key=lambda r: (_rule_axis(r)[0], round(_rule_axis(r)[1]), _rule_axis(r)[2]))
    joined: list[dict] = []
    for rule in out:
        axis, pos, start, end = _rule_axis(rule)
        if joined:
            p_axis, p_pos, p_start, p_end = _rule_axis(joined[-1])
            if p_axis == axis and abs(p_pos - pos) <= slack and start > p_end:
                gap = (p_end, start)
                covered = False
                for panel in panels:
                    x, y, w, h = panel["bbox"]
                    edge_near = (abs(y + h - pos) <= slack or abs(y - pos) <= slack) if axis == "h" else (abs(x - pos) <= slack or abs(x + w - pos) <= slack)
                    span = (x, x + w) if axis == "h" else (y, y + h)
                    if edge_near and span[0] <= gap[0] + slack and span[1] >= gap[1] - slack:
                        covered = True
                        break
                if covered:
                    prev = joined[-1]
                    x1, y1, x2, y2 = prev["points"]
                    joined[-1] = {**prev, "points": [x1, y1, rule["points"][2], y2] if axis == "h" else [x1, y1, x2, rule["points"][3]]}
                    continue
        joined.append(rule)
    return joined


def rules_to_outlines(rules: list[dict], ink: np.ndarray, slack: float = 6.0) -> tuple[list[dict], list[dict]]:
    """Reassemble four rules that form a box into one outline.

    A 1 px light border breaks apart at its anti-aliased corners and is found as
    separate top/bottom/left/right rules. Each top/bottom pair takes the nearest
    vertical rules at its own ends; the corner radius is measured on the ink.
    """
    horizontals = [r for r in rules if _rule_axis(r)[0] == "h"]
    verticals = [r for r in rules if _rule_axis(r)[0] == "v"]
    used: set[int] = set()
    outlines = []
    for top in sorted(horizontals, key=lambda r: _rule_axis(r)[1]):
        if id(top) in used:
            continue
        _, ty, tx0, tx1 = _rule_axis(top)
        for bottom in sorted(horizontals, key=lambda r: _rule_axis(r)[1]):
            _, by, bx0, bx1 = _rule_axis(bottom)
            if id(bottom) in used or by <= ty + 20 or abs(bx0 - tx0) > slack or abs(bx1 - tx1) > slack:
                continue
            reach = 0.3 * min(tx1 - tx0, by - ty) + slack  # a rounded corner shortens the edges

            def side(target: float, outward: int):
                best = None
                for v in verticals:
                    if id(v) in used:
                        continue
                    _, vx, vy0, vy1 = _rule_axis(v)
                    offset = (target - vx) * outward * -1  # how far outside the horizontal's end
                    if -slack <= offset <= reach and vy0 >= ty - slack and vy1 <= by + slack and vy1 - vy0 >= 0.5 * (by - ty):
                        if best is None or abs(offset) < abs(best[0]):
                            best = (offset, v)
                return best[1] if best else None

            left = side(tx0, -1)
            right = side(tx1, 1)
            if left is None or right is None:
                continue
            lx, rx = _rule_axis(left)[1], _rule_axis(right)[1]
            stroke = max(1.0, (top["thickness"] + bottom["thickness"] + left["thickness"] + right["thickness"]) / 4)
            x0, y0 = int(round(lx - stroke / 2)), int(round(ty - stroke / 2))
            x1, y1 = int(round(rx + stroke / 2)), int(round(by + stroke / 2))
            crop = ink[max(0, y0) : y1 + 1, max(0, x0) : x1 + 1]
            # Thicken by a pixel: the diagonal probe must not slip through a stair-stepped 1 px curve.
            radius = corner_radius(dilate(crop, 1)) if y1 > y0 and x1 > x0 else 0.0
            radius = min(radius, 0.5 * min(x1 - x0, y1 - y0))
            darkest = min((top, bottom, left, right), key=lambda r: sum(int(r["color"][i : i + 2], 16) for i in (1, 3, 5)))
            outlines.append(
                {"bbox": [x0, y0, x1 - x0 + 1, y1 - y0 + 1], "line": darkest["color"], "stroke": stroke, "fill": None, "radius": radius}
            )
            used.update(id(r) for r in (top, bottom, left, right))
            break
    return outlines, [r for r in rules if id(r) not in used]


def detect(
    image: np.ndarray, exclude: list[list[float]] | None = None, assets_dir: Path | str | None = None
) -> list[dict]:
    page_h, page_w = image.shape[:2]
    ppx = pt_per_px(page_w, page_h)
    background = border_color(image.astype(np.int32))
    exclude = exclude or []
    min_rule = max(40, int(0.03 * page_w))
    max_rule = max(6, int(0.006 * page_h))
    panels: list[dict] = []
    outlines: list[dict] = []
    rules: list[dict] = []
    img = image.astype(np.int32)
    keep = np.ones((page_h, page_w), dtype=bool)
    for bx, by, bw, bh in exclude:
        keep[max(0, int(by)) : max(0, int(by + bh)), max(0, int(bx)) : max(0, int(bx + bw))] = False

    # Pass 1: flat fills (panels, bars, circles) with a tight color tolerance.
    for color in candidate_colors(image, background, keep=keep):
        mask = np.abs(img - color).max(axis=2) <= color_tolerance(color, background, COLOR_TOLERANCE)
        hex_color = rgb_to_hex(color)
        for comp in label_components(mask):
            x, y, w, h = comp["bbox"]
            if w < MIN_PANEL or h < MIN_PANEL or w * h > 0.9 * page_w * page_h:
                continue
            if inside_any(comp["bbox"], exclude):
                continue
            fill = comp["area"] / max(w * h, 1)
            sub = mask[y : y + h, x : x + w]
            cov = edge_coverage(sub)
            if fill >= 0.55 and min(cov) >= 0.9:
                panels.append({"bbox": [x, y, w, h], "fill": hex_color, "radius": corner_radius(sub)})
            elif 0.68 <= fill <= 0.84 and min(cov) < 0.9 and sub[h // 2, w // 2] and not sub[0, 0] and not sub[-1, -1]:
                panels.append({"bbox": [x, y, w, h], "fill": hex_color, "ellipse": True})

    # Pass 2: strokes (rules, box outlines) among contrasting colors, with a
    # wide tolerance so anti-aliased edges stay connected.
    stroke_colors = candidate_colors(image, background, STROKE_TOLERANCE, STROKE_MIN_CONTRAST, MIN_STROKE_SHARE, keep)
    for color in stroke_colors:
        mask = np.abs(img - color).max(axis=2) <= color_tolerance(color, background, STROKE_TOLERANCE)
        hex_color = rgb_to_hex(color)
        for comp in label_components(mask):
            x, y, w, h = comp["bbox"]
            if w * h > 0.9 * page_w * page_h or inside_any(comp["bbox"], exclude):
                continue
            fill = comp["area"] / max(w * h, 1)
            if h <= max_rule and w >= min_rule and fill >= 0.7:
                cy = y + h / 2
                rules.append({"points": [x, cy, x + w, cy], "color": hex_color, "thickness": comp["area"] / w})
                continue
            if w <= max_rule and h >= min_rule and fill >= 0.7:
                cx = x + w / 2
                rules.append({"points": [cx, y, cx, y + h], "color": hex_color, "thickness": comp["area"] / h})
                continue
            if w < MIN_PANEL or h < MIN_PANEL or fill >= 0.5:
                continue
            sub = mask[y : y + h, x : x + w]
            if min(edge_coverage(sub)) < 0.9:
                continue
            stroke = stroke_width(sub)
            inner = img[y + stroke + 2 : y + h - stroke - 2, x + stroke + 2 : x + w - stroke - 2]
            interior = None
            if inner.size:
                inner_color = border_color(inner)
                if np.abs(inner_color - background).max() > COLOR_TOLERANCE:
                    interior = rgb_to_hex(inner_color)
            outlines.append(
                {"bbox": [x, y, w, h], "line": hex_color, "stroke": stroke, "fill": interior, "radius": corner_radius(sub)}
            )

    # A solid panel sitting just inside an outline of another color is one bordered card.
    for outline in outlines:
        ox, oy, ow, oh = outline["bbox"]
        slack = outline["stroke"] + 3
        for panel in list(panels):
            px, py, pw, ph = panel["bbox"]
            if (
                not panel.get("ellipse")
                and 0 <= px - ox <= slack
                and 0 <= py - oy <= slack
                and 0 <= (ox + ow) - (px + pw) <= slack
                and 0 <= (oy + oh) - (py + ph) <= slack
            ):
                outline["fill"] = panel["fill"]
                panels.remove(panel)

    rules += thin_lines(img, background, keep, min_rule)
    rules = clean_rules(
        rules,
        [(p["bbox"], [p["fill"]]) for p in panels] + [(o["bbox"], [o["line"], o["fill"]]) for o in outlines],
        background,
    )
    rules = [r for r in rules if not is_halo(r, img, stroke_colors, background)]
    ink = np.abs(img - background).max(axis=2) > 1.6 * THIN_CONTRAST
    boxes_from_rules, rules = rules_to_outlines(rules, ink)
    outlines += boxes_from_rules
    rules = join_collinear(rules, panels)
    for rule in rules:
        head = measure_arrow_head(image, rule["points"], rule["color"], rule["thickness"])
        if head is None:
            # Thin-line extraction can stop at the head base when a filled
            # triangle interrupts the shaft. Probe the connected same-colour
            # component farther along the known rule direction.
            x1, y1, x2, y2 = (float(v) for v in rule["points"])
            dist = math.hypot(x2 - x1, y2 - y1)
            if dist >= 8:
                ux, uy = (x2 - x1) / dist, (y2 - y1) / dist
                for extra in (8.0, 16.0, 24.0, 40.0, 64.0):
                    probe = [x1, y1, x2 + ux * extra, y2 + uy * extra]
                    found = measure_arrow_head(image, probe, rule["color"], rule["thickness"])
                    if found:
                        head = found
                        break
        if head:
            rule["arrow"] = head

    objects: list[dict] = []
    shapes = [("panel", p) for p in panels] + [("outline", o) for o in outlines]
    shapes.sort(key=lambda item: -item[1]["bbox"][2] * item[1]["bbox"][3])  # big first = further back
    for index, (kind, s) in enumerate(shapes, start=1):
        radius = s.get("radius", 0.0)
        # Anti-aliasing rounds every corner by 1-3 px; a measured radius above that is real rounding.
        rounded = radius >= 4.5
        obj_type = "ellipse" if s.get("ellipse") else ("round_rect" if rounded else "rect")
        style: dict = {}
        if kind == "panel":
            style.update({"fill": s["fill"], "line": None})
        else:
            # PowerPoint centres the stroke on the geometry edge: inset the box by half a stroke.
            half = s["stroke"] / 2
            x, y, w, h = s["bbox"]
            s["bbox"] = [x + half, y + half, w - 2 * half, h - 2 * half]
            style.update({"fill": s["fill"], "line": s["line"], "line_width_pt": round(s["stroke"] * ppx, 2)})
        if obj_type == "round_rect":
            style["corner_radius_px"] = round(radius, 1)
        objects.append(
            {
                "id": f"shape-{index}",
                "type": obj_type,
                "bbox": [round(v, 1) for v in s["bbox"]],
                # Keep every detected shape below text (z 10) and rules (z 1).
                "z": round(index / (len(shapes) + 1), 4),
                "style": style,
                "note": f"detected {kind}",
            }
        )
    for index, rule in enumerate(sorted(rules, key=lambda r: (r["points"][1], r["points"][0])), start=1):
        decision = rule_arrow_decision(rule)
        if decision["mode"] == "image" and assets_dir:
            hw = rule["arrow"]["head_w_px"]
            min_x = max(0, int(min(rule["points"][0], rule["points"][2]) - hw / 2 - 4))
            min_y = max(0, int(min(rule["points"][1], rule["points"][3]) - hw / 2 - 4))
            max_x = min(image.shape[1], int(max(rule["points"][0], rule["points"][2]) + hw / 2 + 4))
            max_y = min(image.shape[0], int(max(rule["points"][1], rule["points"][3]) + hw / 2 + 4))
            ibox = ink_box(image, [min_x, min_y, max_x, max_y])
            if ibox and ibox[2] > ibox[0] and ibox[3] > ibox[1]:
                adir = Path(assets_dir)
                adir.mkdir(parents=True, exist_ok=True)
                crop_img = Image.fromarray(image[ibox[1]:ibox[3], ibox[0]:ibox[2]])
                asset_filename = f"arrow-{index}.png"
                crop_img.save(adir / asset_filename)
                rel_path = (Path(assets_dir).name + "/" + asset_filename) if not str(assets_dir).startswith("/") else str(adir / asset_filename)
                objects.append(
                    {
                        "id": f"rule-{index}",
                        "type": "image",
                        "bbox": [ibox[0], ibox[1], ibox[2] - ibox[0], ibox[3] - ibox[1]],
                        "path": rel_path,
                        "z": 1,
                        "note": "detected arrow head; direction undetermined, cropped as image",
                    }
                )
                continue
        style = {"color": rule["color"], "width_pt": round(max(rule["thickness"], 1) * ppx, 2)}
        note = "detected rule"
        if decision["mode"] == "preset":
            style.update({"end_arrow": decision["arrow"], "head_w": decision["head_w"], "head_len": decision["head_len"]})
        elif decision["mode"] == "head_size":
            style.update({"end_arrow": decision["arrow"], "head_size_px": decision["head_size_px"]})
            note = "detected arrow head; head/stroke > 6, native triangle"
        elif decision["mode"] == "image":
            note = "detected arrow head; direction undetermined, crop as image"
        objects.append(
            {
                "id": f"rule-{index}",
                "type": "line",
                "points": [round(v, 1) for v in rule["points"]],
                "z": 1,
                "style": style,
                "note": note,
            }
        )
    return objects


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--merge", help="PageIR to add detected objects to (its text boxes are excluded)")
    parser.add_argument("--page", type=int, default=1, help="1-based page index used with --merge")
    parser.add_argument("--assets-dir", help="folder to save cropped oversized arrow images")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    image = np.array(Image.open(args.image).convert("RGB"))

    payload = None
    exclude: list[list[float]] = []
    if args.merge:
        payload = json.loads(Path(args.merge).read_text(encoding="utf-8"))
        pages = payload.get("pages", [])
        if not 1 <= args.page <= len(pages):
            parser.error(f"--page {args.page} is out of range")
        page = pages[args.page - 1]
        if (int(page["width_px"]), int(page["height_px"])) != (image.shape[1], image.shape[0]):
            parser.error("image size does not match the PageIR page size")
        # Text boxes and image assets (icons) are already owned by other objects.
        exclude = [
            o["bbox"]
            for o in page.get("objects", [])
            if o.get("type") in ("text", "image") and isinstance(o.get("bbox"), list)
        ]

    out = Path(args.out)
    assets_dir = None
    if args.assets_dir:
        assets_dir = (out.parent / args.assets_dir) if not Path(args.assets_dir).is_absolute() else Path(args.assets_dir)
    objects = detect(image, exclude, assets_dir=assets_dir)
    if payload is not None:
        page = payload["pages"][args.page - 1]
        taken = {str(o.get("id")) for o in page.get("objects", [])}
        for obj in objects:
            base, n = obj["id"], 1
            while obj["id"] in taken:
                n += 1
                obj["id"] = f"{base}-{n}"
            taken.add(obj["id"])
        page.setdefault("objects", []).extend(objects)
        output = payload
    else:
        output = {"width_px": image.shape[1], "height_px": image.shape[0], "objects": objects}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    counts: dict[str, int] = {}
    for obj in objects:
        counts[obj["type"]] = counts.get(obj["type"], 0) + 1
    print(json.dumps({"output": str(out), "detected": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
