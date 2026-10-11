#!/usr/bin/env python3
"""Sample background, foreground, and dominant colors inside source-pixel boxes.

Examples:
    sample_colors.py source.png --bbox 100 220 360 180 --bbox 54 36 1150 92
    sample_colors.py source.png --page-ir page_ir.json      # report every bbox object
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pageir_common import rgb_to_hex  # noqa: E402


QUANT = 16
FOREGROUND_MIN_DISTANCE = 60.0


def _buckets(pixels: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Quantise RGB pixels; return (bucket keys, counts, mean color per bucket)."""
    q = (pixels // QUANT).astype(np.int32)
    keys = q[:, 0] * 256 * 256 + q[:, 1] * 256 + q[:, 2]
    uniq, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    sums = np.zeros((len(uniq), 3), dtype=np.float64)
    np.add.at(sums, inverse, pixels.astype(np.float64))
    return uniq, counts, sums / counts[:, None]


def sample_box(image: np.ndarray, bbox, top: int = 4) -> dict:
    """Colors inside bbox [x, y, w, h] of an RGB array.

    background: most common color on the box border.
    foreground: most common color clearly distinct from the background (text, strokes, icon ink).
    """
    height, width = image.shape[:2]
    x, y, w, h = (int(round(float(v))) for v in bbox)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"bbox {bbox} does not intersect the image")
    crop = image[y0:y1, x0:x1, :3]
    pixels = crop.reshape(-1, 3)

    ring = max(1, min(2, (y1 - y0) // 4, (x1 - x0) // 4))
    border = np.concatenate(
        [crop[:ring].reshape(-1, 3), crop[-ring:].reshape(-1, 3), crop[:, :ring].reshape(-1, 3), crop[:, -ring:].reshape(-1, 3)]
    )
    _, b_counts, b_means = _buckets(border)
    background = b_means[int(np.argmax(b_counts))]

    _, counts, means = _buckets(pixels)
    order = np.argsort(-counts)
    total = float(counts.sum())
    dominant = [
        {"color": rgb_to_hex(means[i]), "fraction": round(float(counts[i]) / total, 4)} for i in order[:top]
    ]
    foreground = None
    for i in order:
        if float(np.linalg.norm(means[i] - background)) >= FOREGROUND_MIN_DISTANCE:
            foreground = {"color": rgb_to_hex(means[i]), "fraction": round(float(counts[i]) / total, 4)}
            break
    return {
        "bbox": [x0, y0, x1 - x0, y1 - y0],
        "background": rgb_to_hex(background),
        "foreground": foreground["color"] if foreground else None,
        "foreground_fraction": foreground["fraction"] if foreground else 0.0,
        "dominant": dominant,
    }


def page_background(image: np.ndarray) -> str:
    """Most common color along the outer page border."""
    h, w = image.shape[:2]
    return sample_box(image, [0, 0, w, h])["background"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--bbox", nargs=4, type=float, action="append", metavar=("X", "Y", "W", "H"))
    parser.add_argument("--page-ir", help="report colors for every bbox object on --page of this PageIR")
    parser.add_argument("--page", type=int, default=1, help="1-based page index used with --page-ir")
    args = parser.parse_args()
    image = np.array(Image.open(args.image).convert("RGB"))

    report: dict = {"image": args.image, "page_background": page_background(image), "samples": []}
    for bbox in args.bbox or []:
        report["samples"].append(sample_box(image, bbox))
    if args.page_ir:
        pages = json.loads(Path(args.page_ir).read_text(encoding="utf-8")).get("pages", [])
        if not 1 <= args.page <= len(pages):
            parser.error(f"--page {args.page} is out of range")
        for obj in pages[args.page - 1].get("objects", []):
            if isinstance(obj.get("bbox"), list):
                report["samples"].append({"id": obj.get("id"), "type": obj.get("type"), **sample_box(image, obj["bbox"])})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
