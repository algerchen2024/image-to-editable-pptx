#!/usr/bin/env python3
"""Image helpers built only on numpy and Pillow (no OpenCV/SciPy required).

Restricted sandboxes (for example ChatGPT code execution) often lack OpenCV, so
every image operation the skill needs lives here.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter


INK_THRESHOLD = 48


def to_gray(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.float32)
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def blur(gray: np.ndarray, radius: float) -> np.ndarray:
    """Gaussian blur of a 2-D uint8/float array."""
    image = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8))
    return np.asarray(image.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)


def edges(rgb: np.ndarray, threshold: float = 60.0) -> np.ndarray:
    """Boolean edge map from Sobel gradient magnitude on a lightly blurred image."""
    gray = blur(to_gray(rgb), 0.8)
    padded = np.pad(gray, 1, mode="edge")
    gx = (
        padded[:-2, 2:] + 2 * padded[1:-1, 2:] + padded[2:, 2:]
        - padded[:-2, :-2] - 2 * padded[1:-1, :-2] - padded[2:, :-2]
    )
    gy = (
        padded[2:, :-2] + 2 * padded[2:, 1:-1] + padded[2:, 2:]
        - padded[:-2, :-2] - 2 * padded[:-2, 1:-1] - padded[:-2, 2:]
    )
    return np.hypot(gx, gy) > threshold


def dilate(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    out = mask.copy()
    h, w = mask.shape
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx == 0 and dy == 0:
                continue
            ys = slice(max(0, dy), h + min(0, dy))
            yd = slice(max(0, -dy), h + min(0, -dy))
            xs = slice(max(0, dx), w + min(0, dx))
            xd = slice(max(0, -dx), w + min(0, -dx))
            out[yd, xd] |= mask[ys, xs]
    return out


def edge_f1(a: np.ndarray, b: np.ndarray, tolerance: int = 1) -> float:
    """Edge agreement between two RGB images; edges within `tolerance` px count as matches."""
    ea = edges(a)
    eb = edges(b)
    if not ea.any() and not eb.any():
        return 1.0
    precision = np.logical_and(eb, dilate(ea, tolerance)).sum() / max(eb.sum(), 1)
    recall = np.logical_and(ea, dilate(eb, tolerance)).sum() / max(ea.sum(), 1)
    return float(2 * precision * recall / max(precision + recall, 1e-12))


def heat_colors(values: np.ndarray) -> np.ndarray:
    """Map a 0..255 array to a blue -> green -> yellow -> red RGB ramp."""
    v = np.clip(values.astype(np.float32) / 255.0, 0.0, 1.0)
    r = np.clip(2.0 * v - 0.5, 0, 1)
    g = np.clip(1.5 - np.abs(4.0 * v - 2.0), 0, 1)
    b = np.clip(1.0 - 2.0 * v, 0, 1)
    return (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)


def border_color(crop: np.ndarray) -> np.ndarray:
    """Most common (quantised) color along the outer ring of an RGB crop."""
    ring = max(1, min(2, crop.shape[0] // 4, crop.shape[1] // 4))
    border = np.concatenate(
        [
            crop[:ring].reshape(-1, 3),
            crop[-ring:].reshape(-1, 3),
            crop[:, :ring].reshape(-1, 3),
            crop[:, -ring:].reshape(-1, 3),
        ]
    ).astype(np.int32)
    keys = (border // 16) @ np.array([65536, 256, 1])
    values, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    best = int(np.argmax(counts))
    return border[inverse.reshape(-1) == best].mean(axis=0)


def dominant_color(crop: np.ndarray) -> np.ndarray:
    """Most common (quantised) color in an RGB crop: the background behind text."""
    pixels = crop.reshape(-1, 3).astype(np.int32)
    keys = (pixels // 16) @ np.array([65536, 256, 1])
    values, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    return pixels[inverse.reshape(-1) == int(np.argmax(counts))].mean(axis=0)


def ink_box(image: np.ndarray, region, threshold: int = INK_THRESHOLD, ignore_border_ink: bool = False):
    """Bounding box [x0, y0, x1, y1] of pixels that differ from the region's border color.

    With ignore_border_ink, connected ink that touches the region border is
    dropped: it belongs to something larger than the region (an enclosing
    panel outline, a long rule), not to the text being measured.
    Returns None when the region is empty or contains no ink.
    """
    h, w = image.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in region)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    crop = image[y0:y1, x0:x1, :3].astype(np.int32)
    bg = border_color(crop)
    mask = np.abs(crop - bg).max(axis=2) > threshold
    if mask.sum() < 4:
        return None
    if ignore_border_ink:
        mh, mw = mask.shape
        boxes = [
            c["bbox"]
            for c in label_components(mask)
            if c["bbox"][0] > 0 and c["bbox"][1] > 0 and c["bbox"][0] + c["bbox"][2] < mw and c["bbox"][1] + c["bbox"][3] < mh
        ]
        if not boxes:
            return None
        return [
            x0 + min(b[0] for b in boxes),
            y0 + min(b[1] for b in boxes),
            x0 + max(b[0] + b[2] for b in boxes),
            y0 + max(b[1] + b[3] for b in boxes),
        ]
    ys = np.flatnonzero(mask.any(axis=1))
    xs = np.flatnonzero(mask.any(axis=0))
    return [x0 + int(xs[0]), y0 + int(ys[0]), x0 + int(xs[-1]) + 1, y0 + int(ys[-1]) + 1]


def ink_lines(image: np.ndarray, region, threshold: int = INK_THRESHOLD, bg_region=None) -> list[list[int]]:
    """Text-line bands [x0, y0, x1, y1] inside a region, top to bottom.

    The background is the dominant color inside `bg_region` (the text box), so
    white text on a colored header measures correctly; without it, the region
    border color is used. Ink connected to the region border is ignored
    (enclosing panels, rules). Glyph components are grouped into lines by
    vertical overlap.
    """
    h, w = image.shape[:2]
    x0, y0, x1, y1 = (int(round(v)) for v in region)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return []
    crop = image[y0:y1, x0:x1, :3].astype(np.int32)
    bg = None
    if bg_region is not None:
        bx0, by0, bx1, by1 = (int(round(v)) for v in bg_region)
        bx0, by0, bx1, by1 = max(0, bx0), max(0, by0), min(w, bx1), min(h, by1)
        if bx1 - bx0 >= 2 and by1 - by0 >= 2:
            bg = dominant_color(image[by0:by1, bx0:bx1, :3])
    if bg is None:
        bg = border_color(crop)
    mask = np.abs(crop - bg).max(axis=2) > threshold
    if mask.sum() < 4:
        return []
    mh, mw = mask.shape
    boxes = sorted(
        (
            c["bbox"]
            for c in label_components(mask)
            if c["bbox"][0] > 0 and c["bbox"][1] > 0 and c["bbox"][0] + c["bbox"][2] < mw and c["bbox"][1] + c["bbox"][3] < mh
        ),
        key=lambda b: b[1],
    )
    if not boxes:
        return []
    # Pass 1: lines from full-height glyph components only, so fragments of a
    # neighbouring line (CJK radicals, punctuation) cannot stretch a line.
    tallest = max(b[3] for b in boxes)
    main = [b for b in boxes if b[3] >= 0.4 * tallest]
    small = [b for b in boxes if b[3] < 0.4 * tallest]
    lines: list[list[int]] = []
    for bx, by, bw, bh in main:
        top, bottom = by, by + bh
        for line in lines:
            overlap = min(bottom, line[3]) - max(top, line[1])
            if overlap > 0.3 * min(bh, line[3] - line[1]):
                line[0] = min(line[0], bx)
                line[1] = min(line[1], top)
                line[2] = max(line[2], bx + bw)
                line[3] = max(line[3], bottom)
                break
        else:
            lines.append([bx, top, bx + bw, bottom])
    lines.sort(key=lambda b: b[1])
    merged: list[list[int]] = []
    for line in lines:
        if merged and line[1] < merged[-1][3] - 0.5 * (line[3] - line[1]):
            last = merged[-1]
            merged[-1] = [min(last[0], line[0]), min(last[1], line[1]), max(last[2], line[2]), max(last[3], line[3])]
        else:
            merged.append(line)
    # Pass 2: small marks (i-dots, punctuation, a small unit after a big
    # number) join a line only when they sit inside its vertical range; the
    # rest belong to neighbouring text and are dropped. They widen a line but
    # never change its height.
    for bx, by, bw, bh in small:
        for line in merged:
            height = line[3] - line[1]
            if line[1] - 0.25 * height <= by and by + bh <= line[3] + 0.1 * height:
                line[0] = min(line[0], bx)
                line[2] = max(line[2], bx + bw)
                break
    return [[x0 + b[0], y0 + b[1], x0 + b[2], y0 + b[3]] for b in merged]


def label_components(mask: np.ndarray) -> list[dict]:
    """4-connected components of a boolean mask via run-length union-find.

    Returns dicts with bbox [x, y, w, h] and pixel `area`.
    """
    parent: list[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs: list[tuple[int, int, int]] = []  # (row, start, end_exclusive)
    prev_runs: list[int] = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            prev_runs = []
            continue
        padded = np.concatenate(([False], row, [False]))
        diff = np.flatnonzero(padded[1:] != padded[:-1])
        current: list[int] = []
        j = 0
        for start, end in zip(diff[0::2], diff[1::2]):
            idx = len(runs)
            runs.append((y, int(start), int(end)))
            parent.append(idx)
            # Merge with overlapping runs on the previous row.
            while j < len(prev_runs) and runs[prev_runs[j]][2] <= start:
                j += 1
            k = j
            while k < len(prev_runs) and runs[prev_runs[k]][1] < end:
                a, b = find(idx), find(prev_runs[k])
                if a != b:
                    parent[a] = b
                k += 1
            current.append(idx)
        prev_runs = current

    comps: dict[int, list[int]] = {}
    for idx, (y, start, end) in enumerate(runs):
        root = find(idx)
        box = comps.get(root)
        if box is None:
            comps[root] = [start, y, end, y + 1, end - start]
        else:
            box[0] = min(box[0], start)
            box[1] = min(box[1], y)
            box[2] = max(box[2], end)
            box[3] = max(box[3], y + 1)
            box[4] += end - start
    return [{"bbox": [b[0], b[1], b[2] - b[0], b[3] - b[1]], "area": b[4]} for b in comps.values()]
