#!/usr/bin/env python3
"""Combine per-slide PageIR results into one multi-slide deck.

Batch work is done one slide at a time (each with its own fidelity_loop run);
this joins the finished slides afterwards:

    assemble_deck.py work/slide-01/quality/page_ir.best.json work/slide-02/quality/page_ir.best.json \
        --out deck.pptx

All slides must share an aspect ratio (within 0.1%); otherwise deliver separate
decks. The target and font profile of the first slide are kept, and installed
fonts and preferences are merged.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_page_ir import validate_page_ir  # noqa: E402


COMPILER = Path(__file__).resolve().parent / "compile_page_ir.js"


def assemble(paths: list[Path]) -> dict:
    deck: dict | None = None
    base_ratio = None
    for index, path in enumerate(paths, start=1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for page_no, page in enumerate(payload.get("pages", []), start=1):
            ratio = float(page["width_px"]) / float(page["height_px"])
            if base_ratio is None:
                base_ratio = ratio
            elif abs(ratio - base_ratio) / base_ratio > 0.001:
                raise ValueError(f"{path} has a different aspect ratio; deliver it as a separate deck")
            page = json.loads(json.dumps(page))
            page["id"] = f"slide-{index:02d}" + (f"-{page_no}" if page_no > 1 else "")
            for obj in page.get("objects", []):
                if obj.get("type") == "image" and not Path(obj["path"]).is_absolute():
                    obj["path"] = str((path.parent / obj["path"]).resolve())
            if deck is None:
                deck = {k: v for k, v in payload.items() if k != "pages"}
                deck["pages"] = []
                deck.setdefault("target", {"platform": "mac", "installed_fonts": []})
            else:
                target = payload.get("target") or {}
                if target.get("platform") and target.get("platform") != deck["target"].get("platform"):
                    raise ValueError(f"{path} targets {target.get('platform')}, the deck targets {deck['target'].get('platform')}")
                fonts = set(deck["target"].get("installed_fonts", [])) | set(target.get("installed_fonts", []))
                deck["target"]["installed_fonts"] = sorted(fonts)
                prefs = {**target.get("font_preferences", {}), **deck["target"].get("font_preferences", {})}
                if prefs:
                    deck["target"]["font_preferences"] = prefs
            deck["pages"].append(page)
    if deck is None:
        raise ValueError("no pages to assemble")
    return deck


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("page_irs", nargs="+", help="per-slide page_ir.best.json files, in slide order")
    parser.add_argument("--out", required=True, help="deck PPTX path; the combined PageIR is written next to it")
    args = parser.parse_args()
    try:
        deck = assemble([Path(p).resolve() for p in args.page_irs])
    except ValueError as exc:
        parser.error(str(exc))
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    deck_ir = out.with_suffix(".page_ir.json")
    deck_ir.write_text(json.dumps(deck, ensure_ascii=False, indent=2), encoding="utf-8")
    errors = validate_page_ir(deck, deck_ir.parent)
    if errors:
        print(json.dumps({"error": "assembled PageIR is invalid", "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    proc = subprocess.run(["node", str(COMPILER), str(deck_ir), str(out)], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout)
        return proc.returncode
    from inspect_pptx import inspect

    report = inspect(out)
    print(
        json.dumps(
            {"output": str(out), "page_ir": str(deck_ir), "slides": len(deck["pages"]), "inspect_pass": report["pass"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
