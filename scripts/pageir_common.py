#!/usr/bin/env python3
"""Shared helpers for PageIR scripts: colors, slide geometry, and px/pt conversion."""

from __future__ import annotations

import re

# Must match compile_page_ir.js: the long side of the slide is fixed and the
# short side follows the source aspect ratio.
LANDSCAPE_WIDTH_IN = 13.333333
PORTRAIT_HEIGHT_IN = 7.5

HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def is_hex_color(value: object) -> bool:
    return isinstance(value, str) and bool(HEX_RE.match(value))


def normalize_hex(value: str) -> str:
    """Return #RRGGBB (uppercase) for #RGB or #RRGGBB input."""
    if not is_hex_color(value):
        raise ValueError(f"not a hex color: {value!r}")
    digits = value[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.upper()


def rgb_to_hex(rgb) -> str:
    r, g, b = (int(round(float(c))) for c in rgb[:3])
    return "#{:02X}{:02X}{:02X}".format(max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def slide_size_inches(width_px: float, height_px: float) -> tuple[float, float]:
    """Slide size used by the compiler for a page of the given source size."""
    aspect = float(width_px) / float(height_px)
    if aspect >= 1:
        return LANDSCAPE_WIDTH_IN, LANDSCAPE_WIDTH_IN / aspect
    return PORTRAIT_HEIGHT_IN * aspect, PORTRAIT_HEIGHT_IN


def pt_per_px(width_px: float, height_px: float) -> float:
    """Typographic points represented by one source pixel on the compiled slide."""
    _, height_in = slide_size_inches(width_px, height_px)
    return height_in * 72.0 / float(height_px)
