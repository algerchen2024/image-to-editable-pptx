#!/usr/bin/env python3
"""Crop icons and other complex graphics from the source into PageIR image objects.

Run after the text objects exist and before detect_shapes.py:

    extract_assets.py source.png --merge page_ir.json --out page_ir.json --assets-dir assets

Ink that no existing object covers is grouped into clusters. A cluster becomes
an image asset when it is compact (icon-sized) and is not a single clean
primitive: a solid bar or a hairline is left for detect_shapes.py to rebuild
natively, while multi-part or hollow artwork (line icons, logos, glyph-like
symbols) is cropped with a transparent background. detect_shapes.py then
skips these regions, so icons are never half-rebuilt from fragments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crop_asset import crop_asset  # noqa: E402
from detect_shapes import thin_lines  # noqa: E402
from image_ops import border_color, dilate, label_components  # noqa: E402


INK_CONTRAST = 24
MAX_SHARE = 0.18  # an asset spans at most this share of the page width/height
MIN_SIZE = 24
JOIN_PX = 20  # parts of one icon closer than this are one asset


def _object_boxes(page: dict) -> list[list[float]]:
    boxes = []
    for obj in page.get("objects", []):
        if obj.get("type") == "line" and isinstance(obj.get("points"), list):
            x1, y1, x2, y2 = (float(v) for v in obj["points"])
            boxes.append([min(x1, x2) - 3, min(y1, y2) - 3, abs(x2 - x1) + 6, abs(y2 - y1) + 6])
        elif isinstance(obj.get("bbox"), list):
            boxes.append([float(v) for v in obj["bbox"]])
    return boxes


def is_primitive(mask: np.ndarray) -> bool:
    """A single solid block or a hairline: native shapes rebuild these better than pixels."""
    comps = label_components(mask)
    if len(comps) != 1:
        return False
    x, y, w, h = comps[0]["bbox"]
    fill = comps[0]["area"] / max(w * h, 1)
    thin = min(w, h) <= 6 and max(w, h) >= 8 * min(w, h)
    return fill >= 0.85 or thin


def collinear_groups(rules: list[dict], ink: np.ndarray) -> list[tuple[float, list[dict]]]:
    """Group hairline pieces of one line: same row/column, gaps filled with ink (bars resting on an axis).

    Returns (total span, pieces) per group. Pieces separated by background stay
    separate, so the frames of icons that happen to share a row never chain up.
    """
    def key(rule):
        x1, y1, x2, y2 = rule["points"]
        if y1 == y2:
            return "h", y1, min(x1, x2), max(x1, x2)
        return "v", x1, min(y1, y2), max(y1, y2)

    groups: list[list[dict]] = []
    for rule in sorted(rules, key=lambda r: (key(r)[0], round(key(r)[1]), key(r)[2])):
        axis, pos, lo, _ = key(rule)
        if groups:
            p_axis, p_pos, _, p_hi = key(groups[-1][-1])
            if p_axis == axis and abs(p_pos - pos) <= 2 and lo > p_hi:
                y0, y1 = int(pos) - 3, int(pos) + 4
                strip = ink[max(0, y0) : y1, int(p_hi) : int(lo)] if axis == "h" else ink[int(p_hi) : int(lo), max(0, y0) : y1]
                filled = strip.any(axis=0 if axis == "h" else 1).mean() if strip.size else 0.0
                if filled >= 0.9:
                    groups[-1].append(rule)
                    continue
        groups.append([rule])
    return [(key(g[-1])[3] - key(g[0])[2], g) for g in groups]


def find_assets(image: np.ndarray, page: dict) -> list[list[int]]:
    """Bounding boxes [x, y, w, h] of icon-like clusters not covered by existing objects."""
    h, w = image.shape[:2]
    img = image.astype(np.int32)
    ink = np.abs(img - border_color(img)).max(axis=2) > INK_CONTRAST
    sx, sy = w / float(page["width_px"]), h / float(page["height_px"])
    keep = np.ones((h, w), dtype=bool)
    for bx, by, bw, bh in _object_boxes(page):
        keep[max(0, int(by * sy)) : max(0, int((by + bh) * sy) + 1), max(0, int(bx * sx)) : max(0, int((bx + bw) * sx) + 1)] = False
    ink &= keep
    # Card borders and axes are hairlines for detect_shapes.py; remove them so they
    # neither bridge icons into one huge cluster nor leave corner pieces behind.
    background = border_color(img)
    rules = thin_lines(img, background, keep, max(40, int(0.03 * w)))
    for span, group in collinear_groups(rules, ink):
        # Only structure longer than any icon (card borders, axes); an icon's own frame stays.
        horizontal = group[0]["points"][1] == group[0]["points"][3]
        if span <= MAX_SHARE * (w if horizontal else h):
            continue
        for rule in group:
            x1, y1, x2, y2 = rule["points"]
            pad = rule["thickness"] / 2 + 3
            ink[max(0, int(min(y1, y2) - pad)) : int(max(y1, y2) + pad) + 1, max(0, int(min(x1, x2) - pad)) : int(max(x1, x2) + pad) + 1] = False
    # Work at half resolution: joining parts of an icon does not need full detail.
    small = ink[::2, ::2]
    joined = dilate(small, max(1, JOIN_PX // 4))
    boxes = []
    for comp in label_components(joined):
        cx, cy, cw, ch = (v * 2 for v in comp["bbox"])
        if cw < MIN_SIZE or ch < MIN_SIZE or cw > MAX_SHARE * w or ch > MAX_SHARE * h:
            continue
        region = ink[cy : cy + ch, cx : cx + cw]
        if region.sum() < 30 or is_primitive(region):
            continue
        ys = np.flatnonzero(region.any(axis=1))
        xs = np.flatnonzero(region.any(axis=0))
        boxes.append([cx + int(xs[0]), cy + int(ys[0]), int(xs[-1] - xs[0] + 1), int(ys[-1] - ys[0] + 1)])
    return sorted(merge_overlapping(boxes), key=lambda b: (b[1], b[0]))


def merge_overlapping(boxes: list[list[int]]) -> list[list[int]]:
    """Union boxes that overlap (an icon's inner part found as its own cluster)."""
    merged = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i], merged[j]
                if a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]:
                    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
                    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
                    merged[i] = [x0, y0, x1 - x0, y1 - y0]
                    del merged[j]
                    changed = True
                    break
            if changed:
                break
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--merge", required=True, help="PageIR whose text (and any shapes) already exist")
    parser.add_argument("--out", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--assets-dir", default="assets", help="folder for cropped PNGs (relative to the output PageIR)")
    parser.add_argument("--pad", type=int, default=4)
    args = parser.parse_args()

    source = Image.open(args.image).convert("RGB")
    payload = json.loads(Path(args.merge).read_text(encoding="utf-8"))
    pages = payload.get("pages", [])
    if not 1 <= args.page <= len(pages):
        parser.error(f"--page {args.page} is out of range")
    page = pages[args.page - 1]
    image = np.array(source)
    background = border_color(image.astype(np.int32))
    white_bg = bool(np.all(background >= 235))

    out = Path(args.out).resolve()
    assets_dir = (out.parent / args.assets_dir).resolve()
    assets_dir.mkdir(parents=True, exist_ok=True)
    taken = {str(o.get("id")) for o in page.get("objects", [])}
    added = []
    for index, bbox in enumerate(find_assets(image, page), start=1):
        asset, box = crop_asset(source, bbox, pad=args.pad, white_to_alpha=245 if white_bg else 0)
        oid = f"asset-{index}"
        while oid in taken:
            oid += "-x"
        taken.add(oid)
        file = assets_dir / f"{page.get('id', 'page')}-{oid}.png"
        asset.save(file)
        page.setdefault("objects", []).append(
            {
                "id": oid,
                "type": "image",
                "bbox": box,
                "path": os.path.relpath(file, out.parent).replace(os.sep, "/"),
                "z": 5,
                "note": "extracted asset: check it is an icon/graphic, not text that should be editable",
            }
        )
        added.append({"id": oid, "bbox": box})
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "assets": added}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
