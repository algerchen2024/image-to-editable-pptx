#!/usr/bin/env python3
"""Check that every slide went through the full high-fidelity pipeline before delivery.

Each argument is the --workdir one fidelity_loop.py run used for one slide:

    delivery_gate.py work/slide-01/quality work/slide-02/quality

Per slide it requires a fidelity_loop record, a valid PageIR (no font errors),
a structurally clean PPTX, and no render problems. The fidelity level is then:

- HIGH:  object fit >= --min-fit (default 0.9); may be called high fidelity / 1:1;
- CLOSE: checks pass but object fit is lower, or more than 3% of the source's
         visible content is not covered by any PageIR object (something was
         left out); deliver only as a close reconstruction and report it;
- FAIL:  a required step is missing or failed; do not deliver this slide.

Exit status is 0 when no slide FAILs. The printed table goes into the delivery report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_page_ir import validate_page_ir_detailed  # noqa: E402

UNCOVERED_LIMIT = 0.03
INK_THRESHOLD = 60


def _object_boxes(obj: dict) -> list[tuple[list[float], float]]:
    """(box [x, y, w, h], pad) covering an object, including its rotated extent."""
    import math

    if obj.get("type") == "line" and isinstance(obj.get("points"), list):
        x1, y1, x2, y2 = (float(v) for v in obj["points"])
        return [([min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)], 8.0)]
    if not isinstance(obj.get("bbox"), list):
        return []
    x, y, w, h = (float(v) for v in obj["bbox"])
    angle = math.radians(float(obj.get("rotation_deg") or 0))
    if angle:
        cx, cy = x + w / 2, y + h / 2
        rw = abs(w * math.cos(angle)) + abs(h * math.sin(angle))
        rh = abs(w * math.sin(angle)) + abs(h * math.cos(angle))
        x, y, w, h = cx - rw / 2, cy - rh / 2, rw, rh
    return [([x, y, w, h], 4.0)]


def uncovered_ink(source_path: Path, page: dict, measured: list[list[int]] | None = None) -> tuple[float, list[list[int]]]:
    """Share of the source's visible ink that no PageIR object covers, and the largest such regions.

    A rushed reconstruction leaves content out; per-object fit cannot see
    objects that were never created, but this can.
    """
    import numpy as np
    from PIL import Image

    from image_ops import border_color, label_components

    image = np.array(Image.open(source_path).convert("RGB")).astype(np.int32)
    h, w = image.shape[:2]
    ink = np.abs(image - border_color(image)).max(axis=2) > INK_THRESHOLD
    covered = np.zeros_like(ink)
    sx, sy = w / float(page["width_px"]), h / float(page["height_px"])
    boxes = [b for obj in page.get("objects", []) for b in _object_boxes(obj)]
    # Ink the fit report attributed to an object (e.g. a single-line title
    # overflowing its box) is that object's content, not missing content.
    boxes += [([b[0], b[1], b[2] - b[0], b[3] - b[1]], 2.0) for b in measured or []]
    for box, pad in boxes:
        x0 = max(0, int((box[0] - pad) * sx))
        y0 = max(0, int((box[1] - pad) * sy))
        x1 = min(w, int((box[0] + box[2] + pad) * sx) + 1)
        y1 = min(h, int((box[1] + box[3] + pad) * sy) + 1)
        covered[y0:y1, x0:x1] = True
    total = int(ink.sum())
    if total == 0:
        return 0.0, []
    missing = ink & ~covered
    share = float(missing.sum()) / total
    # Largest uncovered regions, on a 4x downsampled mask for speed.
    small = missing[::4, ::4]
    comps = sorted(label_components(small), key=lambda c: -c["area"])
    regions = [[int(v * 4) for v in c["bbox"]] for c in comps[:5] if c["area"] >= 6]
    return share, regions


def check_slide(workdir: Path, min_fit: float) -> dict:
    row: dict = {"workdir": str(workdir), "issues": []}
    summary_path = workdir / "fidelity_summary.json"
    metrics_path = workdir / "final" / "metrics.json"
    best_ir = workdir / "page_ir.best.json"
    if not summary_path.exists():
        row["issues"].append("fidelity_loop.py was not run (no fidelity_summary.json)")
        row["level"] = "FAIL"
        return row
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    row["rounds"] = len(summary.get("rounds", []))
    row["output"] = summary.get("output")

    if not best_ir.exists():
        row["issues"].append("page_ir.best.json is missing")
    else:
        errors, warnings = validate_page_ir_detailed(json.loads(best_ir.read_text(encoding="utf-8")), best_ir.parent)
        if errors:
            row["issues"] += [f"PageIR: {e}" for e in errors]
        row["same_line_warnings"] = sum("sit on one line" in w for w in warnings)

    if not metrics_path.exists():
        row["issues"].append("final/metrics.json is missing")
    else:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        row["issues"] += [f"render: {p}" for p in metrics.get("problems", [])]
        row["object_fit_ratio"] = metrics.get("object_fit_ratio")
        row["edge_f1"] = [round(s["edge_f1"], 3) for s in metrics.get("slides", [])]
        row["objects_off"] = [
            f["id"]
            for s in metrics.get("slides", [])
            for f in s.get("object_fit", {}).get("objects", [])
            if f["status"] not in ("ok", "no_ink", "not_measured")
        ]
        exact = [s for s in (metrics.get("fonts") or {}).get("substitutions", []) if not s.get("substituted")]
        row["exact_fonts"] = [s["requested"] for s in exact]

    sources = summary.get("sources") or []
    if best_ir.exists() and sources:
        pages = json.loads(best_ir.read_text(encoding="utf-8")).get("pages", [])
        shares = []
        fits_by_slide = []
        if metrics_path.exists():
            for s in json.loads(metrics_path.read_text(encoding="utf-8")).get("slides", []):
                fits = s.get("object_fit", {}).get("objects", [])
                fits_by_slide.append(
                    [
                        (str(f.get("id")), f[k])
                        for f in fits
                        for k in ("source_ink", "render_ink")
                        if f.get("status") == "ok" and f.get(k)
                    ]
                )
        for index, (page, source) in enumerate(zip(pages, sources)):
            if Path(source).exists():
                measured = fits_by_slide[index] if index < len(fits_by_slide) else []
                ids = {str(o.get("id")) for o in page.get("objects", [])}
                measured = [b for f, b in measured if f in ids]
                share, regions = uncovered_ink(Path(source), page, measured)
                shares.append(share)
                if share > UNCOVERED_LIMIT:
                    row.setdefault("missing_regions", []).extend(regions)
        if shares:
            row["uncovered_ink"] = round(max(shares), 4)

    output = Path(row["output"]) if row.get("output") else None
    if output is None or not output.exists():
        row["issues"].append("delivered PPTX is missing")
    else:
        from inspect_pptx import inspect

        report = inspect(output)
        if not report["pass"]:
            row["issues"].append("inspect_pptx failed (out-of-canvas shape or screenshot shortcut)")

    if row["issues"]:
        row["level"] = "FAIL"
    elif (row.get("object_fit_ratio") or 0) >= min_fit and (row.get("uncovered_ink") or 0) <= UNCOVERED_LIMIT:
        row["level"] = "HIGH"
    else:
        row["level"] = "CLOSE"
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workdirs", nargs="+", help="fidelity_loop --workdir of each slide, in slide order")
    parser.add_argument("--min-fit", type=float, default=0.9)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = [check_slide(Path(w), args.min_fit) for w in args.workdirs]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print("| # | level | object fit | uncovered ink | edge F1 | rounds | objects off | issues |")
        print("|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(rows, start=1):
            fit = r.get("object_fit_ratio")
            issues = list(r["issues"])
            if r.get("missing_regions"):
                issues.append(f"content not in PageIR at {r['missing_regions']}")
            print(
                f"| {i} | {r['level']} | {fit if fit is not None else '-'} | {r.get('uncovered_ink', '-')} | "
                f"{r.get('edge_f1', '-')} | {r.get('rounds', 0)} | {', '.join(r.get('objects_off', [])[:6]) or '-'} | "
                f"{'; '.join(issues) or '-'} |"
            )
        failed = [i for i, r in enumerate(rows, start=1) if r["level"] == "FAIL"]
        close = [i for i, r in enumerate(rows, start=1) if r["level"] == "CLOSE"]
        print()
        print(f"HIGH {len(rows) - len(failed) - len(close)}, CLOSE {len(close)}, FAIL {len(failed)}")
        if failed:
            print(f"Do not deliver slides {failed}: finish their pipeline first.")
        if close:
            print(f"Slides {close} are close reconstructions, not 1:1; say so and list their objects_off.")
    return 0 if all(r["level"] != "FAIL" for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
