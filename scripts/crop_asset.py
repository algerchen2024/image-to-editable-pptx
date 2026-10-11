#!/usr/bin/env python3
"""Crop a complex visual from the source image into a PageIR-ready PNG asset.

The crop uses the same source-pixel bbox that goes into PageIR, so the asset's
aspect ratio always matches its frame. Optionally turns the near-white
surrounding into transparency so the asset sits cleanly on any background.

Example:
    crop_asset.py source.png --bbox 1200 160 210 210 --out assets/illustration-1.png --white-to-alpha 245
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
from PIL import Image


def crop_asset(image: Image.Image, bbox, pad: int = 0, white_to_alpha: int = 0) -> tuple[Image.Image, list[int]]:
    """Return (asset, final bbox). The bbox is clamped to the image and includes padding."""
    x, y, w, h = (int(round(float(v))) for v in bbox)
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(image.width, x + w + pad)
    y1 = min(image.height, y + h + pad)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"bbox {bbox} does not intersect the image")
    asset = image.convert("RGBA").crop((x0, y0, x1, y1))
    if white_to_alpha:
        rgba = np.array(asset)
        lightness = rgba[:, :, :3].min(axis=2).astype(np.float32)
        # Fully transparent at/above the threshold, soft ramp over 30 levels
        # below it so anti-aliased edges do not get a white halo.
        ramp = np.clip((white_to_alpha - lightness) / 30.0, 0.0, 1.0)
        rgba[:, :, 3] = (rgba[:, :, 3].astype(np.float32) * ramp).astype(np.uint8)
        asset = Image.fromarray(rgba, "RGBA")
    return asset, [x0, y0, x1 - x0, y1 - y0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("X", "Y", "W", "H"))
    parser.add_argument("--out", required=True, help="output PNG path")
    parser.add_argument("--pad", type=int, default=0, help="extra source pixels around the bbox")
    parser.add_argument(
        "--white-to-alpha",
        type=int,
        default=0,
        metavar="THRESHOLD",
        help="make pixels whose darkest channel is >= THRESHOLD transparent (e.g. 245); 0 disables",
    )
    parser.add_argument("--id", default=None, help="object id for the printed PageIR snippet")
    parser.add_argument(
        "--relative-to",
        default=None,
        help="directory of the PageIR file; the snippet path is written relative to it",
    )
    args = parser.parse_args()
    if not 0 <= args.white_to_alpha <= 255:
        parser.error("--white-to-alpha must be between 0 and 255")

    asset, bbox = crop_asset(Image.open(args.image), args.bbox, args.pad, args.white_to_alpha)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    asset.save(out)
    asset_path = args.out
    if args.relative_to:
        asset_path = os.path.relpath(out.resolve(), Path(args.relative_to).resolve()).replace(os.sep, "/")
    snippet = {"id": args.id or out.stem, "type": "image", "bbox": bbox, "path": asset_path, "z": 5}
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
