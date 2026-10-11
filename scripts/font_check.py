#!/usr/bin/env python3
"""Check which fonts the render environment really uses for the fonts a PageIR asks for.

LibreOffice silently substitutes missing fonts, and with no CJK font at all
Chinese text renders as blank boxes. These checks make both visible.

Example:
    font_check.py page_ir.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pageir_common import FONT_STYLE_NAMES, OFFICE_FONTS, PLATFORM_FONTS, choose_font  # noqa: E402


CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

# Open fonts designed with identical advance widths; a render with these keeps
# line lengths exactly, so geometry tuned against it holds in PowerPoint.
METRIC_COMPATIBLE = {
    "arial": {"liberation sans", "arimo"},
    "helvetica": {"liberation sans", "arimo"},
    "times new roman": {"liberation serif", "tinos"},
    "courier new": {"liberation mono", "cousine"},
    "calibri": {"carlito"},
    "cambria": {"caladea"},
}


def _run(args: list[str]) -> str | None:
    exe = shutil.which(args[0])
    if not exe:
        return None
    proc = subprocess.run([exe, *args[1:]], capture_output=True, text=True, check=False, timeout=20)
    return proc.stdout if proc.returncode == 0 else None


def cjk_font_families() -> list[str] | None:
    """Families that cover Chinese, or None when fontconfig is unavailable."""
    out = _run(["fc-list", ":lang=zh", "family"])
    if out is None:
        return None
    families = set()
    for line in out.splitlines():
        for name in line.split(","):
            if name.strip():
                families.add(name.strip())
    return sorted(families)


def render_families() -> set[str] | None:
    """Every family name (including localized names) the render environment has, or None without fontconfig."""
    out = _run(["fc-list", ":", "family"])
    if out is None:
        return None
    names: set[str] = set()
    for line in out.splitlines():
        names.update(n.strip().replace("\\-", "-") for n in line.split(",") if n.strip())
    return names


def font_plan(platform: str, installed: set[str], preferences: dict | None = None) -> dict:
    """Which target-machine fonts the render environment also has, and the font to use per style.

    `exact_match_fonts` are fonts on the user's machine (platform standard,
    Office, or user-installed) that this environment can render: text in them
    is verified exactly as the user will see it.
    """
    families = render_families()
    if families is None:
        return {"fontconfig_available": False}
    lowered = {f.lower() for f in families}
    platform_key = platform if platform in PLATFORM_FONTS else "mac"
    target_fonts = PLATFORM_FONTS[platform_key] | OFFICE_FONTS | set(installed)
    exact = sorted(f for f in target_fonts if f.lower() in lowered)
    recommended = {}
    for style in FONT_STYLE_NAMES:
        font, is_exact = choose_font(style, platform_key, installed, preferences, families)
        recommended[style] = {"font": font, "exact_render": is_exact}
    return {
        "fontconfig_available": True,
        "target_platform": platform_key,
        "installed_fonts": sorted(installed),
        "exact_match_fonts": exact,
        "recommended": recommended,
    }


def resolve_font(name: str) -> str | None:
    """Family fontconfig would actually use for `name`, or None when fontconfig is unavailable."""
    out = _run(["fc-match", "-f", "%{family[0]}", name])
    return out.strip() if out else None


def page_ir_fonts(payload: dict) -> tuple[set[str], bool]:
    """(font faces used, whether any text contains CJK characters)."""
    fonts: set[str] = set()
    has_cjk = False
    default = payload.get("default_font_face", "Arial")
    for page in payload.get("pages", []):
        for obj in page.get("objects", []):
            if obj.get("type") != "text":
                continue
            style = obj.get("style", {})
            fonts.add(style.get("font_face") or default)
            texts = [obj.get("text") or ""]
            for run in obj.get("runs") or []:
                texts.append(run.get("text") or "")
                if run.get("font_face"):
                    fonts.add(run["font_face"])
            if any(CJK_RE.search(t) for t in texts):
                has_cjk = True
    return fonts, has_cjk


def font_report(payload: dict) -> dict:
    fonts, has_cjk = page_ir_fonts(payload)
    cjk = cjk_font_families()
    substitutions = []
    for name in sorted(fonts):
        resolved = resolve_font(name)
        substituted = resolved is not None and resolved.lower() != name.lower()
        compatible = substituted and resolved.lower() in METRIC_COMPATIBLE.get(name.lower(), set())
        substitutions.append(
            {
                "requested": name,
                "rendered_with": resolved,
                "substituted": substituted,
                "metric_compatible": compatible,
            }
        )
    problems = []
    if has_cjk and cjk is not None and not cjk:
        problems.append(
            "PageIR contains Chinese text but the render environment has no CJK font; renders will show blank "
            "or boxed glyphs. Install a CJK font (e.g. Noto Sans CJK / Source Han Sans) for rendering only; "
            "do not change font_face in PageIR."
        )
    return {
        "fontconfig_available": cjk is not None,
        "has_cjk_text": has_cjk,
        "cjk_fonts": cjk,
        "substitutions": substitutions,
        "problems": problems,
        "note": "Substitution only affects verification renders; the PPTX keeps the requested font names.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("page_ir")
    args = parser.parse_args()
    report = font_report(json.loads(Path(args.page_ir).read_text(encoding="utf-8")))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not report["problems"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
