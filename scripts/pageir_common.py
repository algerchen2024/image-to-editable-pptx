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
        "Songti SC", "STSong", "Kaiti SC", "STKaiti", "STFangsong", "Yuanti SC",
        "Helvetica", "Helvetica Neue", "Arial", "Arial Black", "Arial Narrow", "Avenir", "Avenir Next",
        "Futura", "Gill Sans", "Georgia", "Times New Roman", "Verdana", "Tahoma", "Trebuchet MS",
        "Courier New", "Menlo", "Monaco", "Impact",
    },
    "windows": {
        "Microsoft YaHei", "Microsoft YaHei UI", "DengXian", "SimHei", "SimSun", "NSimSun", "KaiTi", "FangSong",
        "Microsoft JhengHei", "Arial", "Arial Black", "Arial Narrow", "Segoe UI", "Times New Roman", "Georgia",
        "Verdana", "Tahoma", "Trebuchet MS", "Courier New", "Impact", "Bahnschrift",
        # Chinese fonts installed with Microsoft Office on Windows.
        "STKaiti", "STSong", "STFangsong", "STXihei", "STZhongsong",
    },
}
# Installed with Microsoft Office on both platforms.
OFFICE_FONTS = {"Calibri", "Calibri Light", "Cambria", "Candara", "Consolas", "Constantia", "Corbel", "Aptos"}

DEFAULT_FONTS = {
    "mac": {"cjk": "PingFang SC", "latin": "Helvetica Neue"},
    "windows": {"cjk": "Microsoft YaHei", "latin": "Arial"},
}

# Candidate fonts per typeface style, best first. "sans" is 黑体 (Hei), "kai" 楷体,
# "song" 宋体/明体, "fangsong" 仿宋; the latin_* styles are for Latin text and numbers.
FONT_STYLES = {
    "mac": {
        "sans": ["PingFang SC", "Hiragino Sans GB", "Heiti SC"],
        "kai": ["Kaiti SC", "STKaiti"],
        "song": ["Songti SC", "STSong"],
        "fangsong": ["STFangsong"],
        "latin_sans": ["Helvetica Neue", "Arial"],
        "latin_serif": ["Times New Roman", "Georgia"],
    },
    "windows": {
        "sans": ["Microsoft YaHei", "DengXian", "SimHei"],
        "kai": ["KaiTi", "STKaiti"],
        "song": ["SimSun", "STSong"],
        "fangsong": ["FangSong", "STFangsong"],
        "latin_sans": ["Arial", "Segoe UI"],
        "latin_serif": ["Times New Roman", "Georgia"],
    },
}
FONT_STYLE_NAMES = ("sans", "kai", "song", "fangsong", "latin_sans", "latin_serif")


STYLE_KEYWORDS = [
    ("fangsong", ("fangsong", "仿宋")),
    ("kai", ("kai", "楷")),
    ("song", ("song", "ming", "宋", "明")),
    ("sans", ("hei", "yahei", "pingfang", "sans", "黑", "苹方", "雅黑")),
]


def infer_style(name: str) -> str | None:
    """Typeface style from a CJK font name (STKaiti -> kai, Microsoft YaHei -> sans)."""
    lowered = name.lower()
    for style, keywords in STYLE_KEYWORDS:
        if any(k in lowered for k in keywords):
            return style
    return None


def choose_font(
    style: str,
    platform: str,
    installed: set[str] | None = None,
    preferences: dict | None = None,
    render_families: set[str] | None = None,
) -> tuple[str, bool]:
    """Pick the font for a typeface style on the target machine.

    Returns (font, exact) where exact means the render environment has the same
    font, so verification renders show what the user will see. Order:
    1. the user's stated preference for this style;
    2. a font of this style on the target machine (platform standard, or
       user-installed and recognised by name) that the render environment also has;
    3. the platform's first choice for the style (rendered with a substitute).
    """
    platform = platform if platform in FONT_STYLES else "mac"
    render = {f.lower() for f in render_families or set()}
    preferences = preferences or {}
    if preferences.get(style):
        name = str(preferences[style])
        return name, name.lower() in render
    candidates = list(FONT_STYLES[platform].get(style, FONT_STYLES[platform]["sans"]))
    candidates += sorted(f for f in installed or set() if f not in candidates and infer_style(f) == style)
    for name in candidates:
        if name.lower() in render:
            return name, True
    return candidates[0], False


RENDER_ONLY_FONT_RE = re.compile(
    r"^(noto|wenquanyi|wqy|dejavu|liberation|droid|unifont|ar pl|ukai|uming|arimo|tinos|cousine|carlito|caladea)",
    re.IGNORECASE,
)


def target_preferences(payload: dict) -> dict:
    """User-stated font per style, e.g. {"sans": "Microsoft YaHei", "kai": "STKaiti"}."""
    prefs = (payload.get("target") or {}).get("font_preferences") or {}
    return {k: str(v) for k, v in prefs.items() if k in FONT_STYLE_NAMES and v}


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
