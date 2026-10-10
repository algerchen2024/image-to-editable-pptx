#!/usr/bin/env python3
"""Install font files into the render environment so verification renders use the delivery font.

The fidelity loop can only tune text 1:1 when the render uses the same font the
user's PowerPoint will use. Install that font here first:

    install_fonts.py uploads/SourceHanSansSC-Bold.otf uploads/SourceHanSansSC-Heavy.otf
    install_fonts.py uploads/fonts/            # every .ttf/.otf/.ttc in a folder

Only install fonts you are licensed to use this way. Open-licensed families that
also install on macOS and Windows (Source Han Sans / 思源黑体, Noto Sans CJK,
HarmonyOS Sans, Alibaba PuHuiTi) are the safe choice for exact 1:1 work. Apple
system fonts such as PingFang are licensed for Apple devices only; do not copy
them into a Linux sandbox.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path


FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
FONT_DIR = Path.home() / ".local" / "share" / "fonts" / "image-to-editable-pptx"


def font_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.suffix.lower() in FONT_SUFFIXES)
        elif path.suffix.lower() in FONT_SUFFIXES and path.is_file():
            files.append(path)
    return files


def families(path: Path) -> list[str]:
    exe = shutil.which("fc-scan")
    if not exe:
        return []
    proc = subprocess.run([exe, "--format", "%{family}\\n", str(path)], capture_output=True, text=True, check=False)
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        names.update(n.strip() for n in line.split(",") if n.strip())
    return sorted(names)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", help="font files or folders containing them")
    parser.add_argument("--dest", default=str(FONT_DIR))
    args = parser.parse_args()

    files = font_files(args.paths)
    if not files:
        parser.error("no .ttf/.otf/.ttc/.otc files found")
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    installed = []
    for src in files:
        target = dest / src.name
        shutil.copy2(src, target)
        installed.append({"file": src.name, "families": families(target)})

    cache = shutil.which("fc-cache")
    if cache:
        subprocess.run([cache, "-f", str(dest)], capture_output=True, check=False)
    print(
        json.dumps(
            {
                "installed": installed,
                "font_dir": str(dest),
                "fontconfig_refreshed": bool(cache),
                "next": "Use one of these family names as font_face, and list it in target.installed_fonts once the "
                "user has the same font on their Mac/PC.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
