#!/usr/bin/env python3
"""Check the local runtime needed by the image-to-editable-PPTX workflow."""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from font_check import cjk_font_families, font_plan  # noqa: E402


PYTHON_MODULES = {
    "numpy": "numpy",
    "pillow": "PIL",
    "python-pptx": "pptx",
    "pytesseract": "pytesseract",
    "PyYAML": "yaml",
}


def check_module(distribution: str, module: str) -> dict[str, Any]:
    try:
        loaded = importlib.import_module(module)
        return {
            "available": True,
            "distribution": distribution,
            "module": module,
            "version": str(getattr(loaded, "__version__", "unknown")),
        }
    except Exception as exc:
        return {
            "available": False,
            "distribution": distribution,
            "module": module,
            "error": f"{type(exc).__name__}: {exc}",
        }


def tesseract_languages(executable: str | None) -> list[str]:
    if not executable:
        return []
    proc = subprocess.run(
        [executable, "--list-langs"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=20,
    )
    return sorted(
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not line.lower().startswith("list of available languages")
    )


def node_has_pptxgenjs(node: str | None) -> dict[str, Any]:
    if not node:
        return {"available": False, "error": "node not found"}
    proc = subprocess.run(
        [node, "-e", "const P=require('pptxgenjs'); const p=new P(); console.log(P.version||p.version||'available')"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=20,
    )
    if proc.returncode != 0:
        return {"available": False, "error": proc.stdout.strip()}
    return {"available": True, "version": proc.stdout.strip() or "available"}


def build_report(ocr_lang: str, target: str = "mac", installed: set[str] | None = None) -> dict[str, Any]:
    modules = {name: check_module(name, module) for name, module in PYTHON_MODULES.items()}
    executables = {name: shutil.which(name) for name in ("python3", "node", "tesseract", "pdftoppm", "soffice", "libreoffice")}
    langs = tesseract_languages(executables["tesseract"])
    requested_langs = [item for item in ocr_lang.split("+") if item]
    missing_langs = [item for item in requested_langs if item not in langs]
    pptxgenjs = node_has_pptxgenjs(executables["node"])
    renderer_available = bool(executables["soffice"] or executables["libreoffice"])
    missing_python = [name for name, status in modules.items() if not status["available"]]
    ready_core = not missing_python and bool(executables["node"]) and pptxgenjs["available"]
    compare_modules = ("numpy", "pillow", "python-pptx")
    ready_compare = (
        renderer_available
        and bool(executables["pdftoppm"])
        and all(modules[name]["available"] for name in compare_modules)
    )
    cjk_fonts = cjk_font_families()
    return {
        "ready_core": ready_core,
        "ready_render_compare": ready_compare,
        "python_modules": modules,
        "executables": executables,
        "pptxgenjs": pptxgenjs,
        "ocr_requested": requested_langs,
        "ocr_languages_missing": missing_langs,
        "cjk_fonts": cjk_fonts,
        "cjk_render_ready": bool(cjk_fonts),
        "fonts": font_plan(target, installed or set()),
        "notes": [
            "No OpenCV needed: render comparison and fidelity_loop use numpy + Pillow only.",
            "cjk_render_ready=false means Chinese renders blank; install a CJK font (Noto Sans CJK, "
            "Source Han Sans, WenQuanYi) for verification renders before comparing. Keep PageIR font_face unchanged.",
            "OCR language packs are optional if text is transcribed by another trusted method.",
            "LibreOffice and pdftoppm are needed only for the provided render-comparison script.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-lang", default="eng", help="Tesseract language expression, e.g. chi_sim+eng")
    parser.add_argument("--target", choices=["mac", "windows"], default="mac", help="platform that opens the PPTX")
    parser.add_argument(
        "--installed-fonts",
        default="",
        help="comma-separated fonts the user has installed on that machine, e.g. 'Microsoft YaHei,STKaiti'",
    )
    args = parser.parse_args()
    installed = {f.strip() for f in args.installed_fonts.split(",") if f.strip()}
    report = build_report(args.ocr_lang, args.target, installed)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_core"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
