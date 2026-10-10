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


# ---------------------------------------------------------------- fonts
#
# The PPTX is opened on the user's machine, not where it was rendered for
# verification. Fonts must therefore come from the target platform; fonts that
# only exist in Linux render sandboxes must never be written into PageIR unless
# the user has installed them too.

PLATFORM_FONTS = {
    "mac": {
        "PingFang SC", "PingFang TC", "PingFang HK", "Hiragino Sans GB", "Heiti SC", "Heiti TC", "STHeiti",
        "Songti SC", "STSong", "Kaiti SC", "STKaiti", "Yuanti SC",
        "Helvetica", "Helvetica Neue", "Arial", "Arial Black", "Arial Narrow", "Avenir", "Avenir Next",
        "Futura", "Gill Sans", "Georgia", "Times New Roman", "Verdana", "Tahoma", "Trebuchet MS",
        "Courier New", "Menlo", "Monaco", "Impact",
    },
    "windows": {
        "Microsoft YaHei", "Microsoft YaHei UI", "DengXian", "SimHei", "SimSun", "NSimSun", "KaiTi", "FangSong",
        "Microsoft JhengHei", "Arial", "Arial Black", "Arial Narrow", "Segoe UI", "Times New Roman", "Georgia",
        "Verdana", "Tahoma", "Trebuchet MS", "Courier New", "Impact", "Bahnschrift",
    },
}
# Installed with Microsoft Office on both platforms.
OFFICE_FONTS = {"Calibri", "Calibri Light", "Cambria", "Candara", "Consolas", "Constantia", "Corbel", "Aptos"}

DEFAULT_FONTS = {
    "mac": {"cjk": "PingFang SC", "latin": "Helvetica Neue"},
    "windows": {"cjk": "Microsoft YaHei", "latin": "Arial"},
}

RENDER_ONLY_FONT_RE = re.compile(
    r"^(noto|wenquanyi|wqy|dejavu|liberation|droid|unifont|ar pl|ukai|uming|arimo|tinos|cousine|carlito|caladea)",
    re.IGNORECASE,
)


def target_of(payload: dict) -> tuple[str, set[str]]:
    """(platform, fonts the user has additionally installed) from PageIR `target`."""
    target = payload.get("target") or {}
    platform = str(target.get("platform") or "any").lower()
    installed = {str(f) for f in target.get("installed_fonts") or []}
    return platform, installed


def font_issue(name: str, platform: str, installed: set[str]) -> tuple[str, str] | None:
    """('error'|'warning', message) when `name` will not exist on the user's machine."""
    lowered = {f.lower() for f in installed}
    if name.lower() in lowered:
        return None
    if RENDER_ONLY_FONT_RE.match(name):
        suggestion = DEFAULT_FONTS.get(platform, DEFAULT_FONTS["mac"])
        return (
            "error",
            f"font_face '{name}' is a render-sandbox font, not one on the user's machine; PowerPoint will "
            f"substitute it and the layout will shift. Use '{suggestion['cjk']}' / '{suggestion['latin']}' "
            f"(target {platform}), or add it to target.installed_fonts if the user has installed it.",
        )
    if platform in PLATFORM_FONTS:
        known = {f.lower() for f in PLATFORM_FONTS[platform] | OFFICE_FONTS}
        if name.lower() not in known:
            return ("warning", f"font_face '{name}' is not a standard {platform} font; PowerPoint may substitute it")
    return None


def page_ir_font_names(payload: dict) -> set[str]:
    names = set()
    if payload.get("default_font_face"):
        names.add(str(payload["default_font_face"]))
    for page in payload.get("pages") or []:
        objects = page.get("objects") if isinstance(page, dict) else None
        for obj in objects if isinstance(objects, list) else []:
            if not isinstance(obj, dict) or obj.get("type") != "text":
                continue
            style = obj.get("style") or {}
            if isinstance(style, dict) and style.get("font_face"):
                names.add(str(style["font_face"]))
            for run in obj.get("runs") or []:
                if isinstance(run, dict) and run.get("font_face"):
                    names.add(str(run["font_face"]))
    return names
