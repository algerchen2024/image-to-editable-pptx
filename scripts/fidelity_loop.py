#!/usr/bin/env python3
"""Compile -> render -> measure -> refine until the PPTX matches the source 1:1.

Example:
    fidelity_loop.py page_ir.json source.png --out output.pptx --workdir quality --rounds 6
    fidelity_loop.py page_ir.json page1.png page2.png --out output.pptx

Each round compiles the current PageIR, renders it, measures every object
against the source, and applies the corrections. The best round (most objects
within tolerance, then lowest pixel error) is kept: its PageIR is written to
<workdir>/page_ir.best.json and its deck to --out. The loop stops early when
every measured object is within tolerance or a round brings no improvement.

The loop corrects geometry, font size and letter spacing. It cannot fix wrong
wording, a wrong font family, missing objects, or wrong colors; review those
from the overlay and hotspots it reports.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from object_fit import refine_payload, substituted_fonts  # noqa: E402
from render_compare import run_compare  # noqa: E402
from validate_page_ir import validate_page_ir  # noqa: E402


COMPILER = Path(__file__).resolve().parent / "compile_page_ir.js"
PATIENCE = 2


def compile_ir(payload: dict, ir_path: Path, pptx_path: Path) -> None:
    ir_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(COMPILER), str(ir_path), str(pptx_path)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"compile failed: {proc.stderr or proc.stdout}")


def score(report: dict) -> tuple[float, float]:
    """Higher is better: (object fit ratio, -mean MAE)."""
    slides = report["slides"]
    mae = sum(s["normalized_mae"] for s in slides) / max(len(slides), 1)
    fit = report.get("object_fit_ratio")
    return (fit if fit is not None else 0.0, -mae)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("page_ir")
    parser.add_argument("sources", nargs="+", help="source image per slide, in order")
    parser.add_argument("--out", required=True, help="final PPTX path")
    parser.add_argument("--workdir", default="quality")
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()

    ir_path = Path(args.page_ir).resolve()
    payload = json.loads(ir_path.read_text(encoding="utf-8"))
    errors = validate_page_ir(payload, ir_path.parent)
    if errors:
        print(json.dumps({"error": "PageIR is invalid", "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    sources = [Path(p).resolve() for p in args.sources]
    # Work next to the original PageIR so relative image paths keep resolving.
    round_ir = ir_path.parent / f".{ir_path.stem}.round.json"

    history = []
    best = None
    stale = 0
    try:
        for round_no in range(1, args.rounds + 1):
            pptx = workdir / f"round-{round_no}.pptx"
            compile_ir(payload, round_ir, pptx)
            report = run_compare(sources, pptx, workdir / f"round-{round_no}", payload, write_images=False)
            current = score(report)
            history.append(
                {
                    "round": round_no,
                    "object_fit_ratio": report.get("object_fit_ratio"),
                    "mean_mae": round(-current[1], 5),
                    "edge_f1": [round(s["edge_f1"], 4) for s in report["slides"]],
                }
            )
            if best is None or current > best[0]:
                best = (current, json.loads(json.dumps(payload)), pptx, report)
                stale = 0
            else:
                stale += 1
                if stale >= PATIENCE:
                    break  # corrections stopped helping
            if report.get("object_fit_ratio") == 1.0:
                break
            payload, changed = refine_payload(payload, report["slides"], substituted_fonts(report))
            history[-1]["changed"] = changed
            if not changed:
                break
    finally:
        round_ir.unlink(missing_ok=True)

    assert best is not None
    _, best_payload, best_pptx, best_report = best
    best_ir = workdir / "page_ir.best.json"
    best_ir.write_text(json.dumps(best_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy2(best_pptx, args.out)
    # Final artefacts (overlay, heatmap, metrics) for the delivered deck.
    final = run_compare(sources, Path(args.out), workdir / "final", best_payload)
    (workdir / "final" / "metrics.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "output": args.out,
        "best_page_ir": str(best_ir),
        "rounds": history,
        "object_fit_ratio": final.get("object_fit_ratio"),
        "slides": [
            {
                "slide": s["slide"],
                "normalized_mae": round(s["normalized_mae"], 4),
                "edge_f1": round(s["edge_f1"], 4),
                "objects_off": [
                    f["id"] for f in s.get("object_fit", {}).get("objects", []) if f["status"] not in ("ok", "no_ink", "not_measured")
                ],
            }
            for s in final["slides"]
        ],
        "problems": final["problems"],
        "next": "Copy page_ir.best.json over your PageIR, then review final/overlay.png and the objects_off list.",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not final["problems"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
