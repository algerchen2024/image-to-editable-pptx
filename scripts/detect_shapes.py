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
from image_ops import border_color, label_components  # noqa: E402
from pageir_common import pt_per_px, rgb_to_hex  # noqa: E402


COLOR_TOLERANCE = 14  # flat fills render uniformly
STROKE_TOLERANCE = 44  # thin strokes are anti-aliased across several shades
STROKE_MIN_CONTRAST = 60  # pale tints are fills, not strokes
MIN_COLOR_SHARE = 0.0008
MIN_STROKE_SHARE = 0.0001  # a 1 px rule is only a few hundred pixels
MAX_COLORS = 16
MIN_PANEL = 20


def candidate_colors(
    image: np.ndarray,
    background: np.ndarray,
    tolerance: int = COLOR_TOLERANCE,
    min_contrast: int = COLOR_TOLERANCE,
    min_share: float = MIN_COLOR_SHARE,
) -> list[np.ndarray]:
    """Frequent colors that differ from the page background, deduplicated within `tolerance`."""
    pixels = image.reshape(-1, 3).astype(np.int32)
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
        if any(np.abs(color - c).max() <= tolerance for c in colors):
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
        best = max(group, key=contrast)
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


def detect(image: np.ndarray, exclude: list[list[float]] | None = None) -> list[dict]:
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

    # Pass 1: flat fills (panels, bars, circles) with a tight color tolerance.
    for color in candidate_colors(image, background):
        mask = np.abs(img - color).max(axis=2) <= COLOR_TOLERANCE
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
    for color in candidate_colors(image, background, STROKE_TOLERANCE, STROKE_MIN_CONTRAST, MIN_STROKE_SHARE):
        mask = np.abs(img - color).max(axis=2) <= STROKE_TOLERANCE
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

    rules = clean_rules(
        rules,
        [(p["bbox"], [p["fill"]]) for p in panels] + [(o["bbox"], [o["line"], o["fill"]]) for o in outlines],
        background,
    )

    objects: list[dict] = []
    shapes = [("panel", p) for p in panels] + [("outline", o) for o in outlines]
    shapes.sort(key=lambda item: -item[1]["bbox"][2] * item[1]["bbox"][3])  # big first = further back
    for index, (kind, s) in enumerate(shapes, start=1):
        radius = s.get("radius", 0.0)
        obj_type = "ellipse" if s.get("ellipse") else ("round_rect" if radius >= 3 else "rect")
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
        objects.append(
            {
                "id": f"rule-{index}",
                "type": "line",
                "points": [round(v, 1) for v in rule["points"]],
                "z": 1,
                "style": {"color": rule["color"], "width_pt": round(max(rule["thickness"], 1) * ppx, 2)},
                "note": "detected rule",
            }
        )
    return objects


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--merge", help="PageIR to add detected objects to (its text boxes are excluded)")
    parser.add_argument("--page", type=int, default=1, help="1-based page index used with --merge")
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
        exclude = [o["bbox"] for o in page.get("objects", []) if o.get("type") == "text" and isinstance(o.get("bbox"), list)]

    objects = detect(image, exclude)
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
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    counts: dict[str, int] = {}
    for obj in objects:
        counts[obj["type"]] = counts.get(obj["type"], 0) + 1
    print(json.dumps({"output": str(out), "detected": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
