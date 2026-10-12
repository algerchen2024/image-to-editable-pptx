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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from object_fit import object_error, refine_payload, substituted_fonts  # noqa: E402
from render_compare import run_compare  # noqa: E402
from validate_page_ir import validate_page_ir  # noqa: E402


from compile_page_ir import compile_payload  # noqa: E402
PATIENCE = 2


def compile_ir(payload: dict, ir_path: Path, pptx_path: Path) -> None:
    """Compile with the Python compiler; relative assets resolve against the PageIR's folder."""
    ir_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        compile_payload(json.loads(json.dumps(payload)), ir_path.parent, pptx_path)
    except (ValueError, KeyError, OSError) as exc:
        raise RuntimeError(f"compile failed: {exc}") from exc


def mean_object_error(report: dict) -> float:
    """Average per-object error in px (capped), so partial progress counts between rounds."""
    errors = []
    for slide in report["slides"]:
        for fit in slide.get("object_fit", {}).get("objects", []):
            if fit["status"] in ("ok", "off", "missing_in_render", "extra_in_render"):
                errors.append(min(object_error(fit), 50.0))
    return sum(errors) / len(errors) if errors else 0.0


def score(report: dict) -> tuple[float, float, float]:
    """Higher is better: (object fit ratio, -mean object error, -mean MAE)."""
    slides = report["slides"]
    mae = sum(s["normalized_mae"] for s in slides) / max(len(slides), 1)
    fit = report.get("object_fit_ratio")
    return (fit if fit is not None else 0.0, -round(mean_object_error(report), 2), -mae)


def _font_sizes(obj: dict) -> tuple:
    style = obj.get("style") or {}
    sizes = [style.get("font_size_pt")]
    for run in obj.get("runs") or []:
        if isinstance(run, dict):
            sizes.append(run.get("font_size_pt"))
    return tuple(sizes)


def apply_rollbacks(
    payload: dict,
    slides: list[dict],
    pending: dict,
    strikes: dict[str, int],
    frozen: set[str],
) -> tuple[list[str], list[str], dict]:
    """Restore objects whose last edit got worse. Freeze only on the second strike.

    The returned limits apply on the next refine, after the restored object is
    measured again: a reverted font-size edit may only move, and a reverted
    shape or line takes a half step. Nothing is written onto the PageIR object.
    """
    rolled: list[str] = []
    newly_frozen: list[str] = []
    limits: dict[str, dict] = {}
    for slide in slides:
        page_index = int(slide["slide"]) - 1
        pages = payload.get("pages", [])
        if page_index < 0 or page_index >= len(pages):
            continue
        page = pages[page_index]
        page_id = str(page.get("id", f"slide-{slide['slide']}"))
        fits = {str(f.get("id")): f for f in (slide.get("object_fit") or {}).get("objects", [])}
        for key, item in list(pending.items()):
            if not key.startswith(page_id + ":"):
                continue
            oid = key.split(":", 1)[1]
            now = fits.get(oid)
            if now is None:
                continue
            before = item["fit"]
            current = next((obj for obj in page.get("objects", []) if str(obj.get("id")) == oid), None)
            size_changed = bool(item.get("size_changed")) or (
                current is not None
                and current.get("type") == "text"
                and _font_sizes(current) != _font_sizes(item["object"])
            )
            size_improved = (
                size_changed
                and before.get("scale_h") is not None
                and now.get("scale_h") is not None
                and abs(float(now["scale_h"]) - 1.0) < abs(float(before["scale_h"]) - 1.0)
            )
            # A font-size correction is useful even when its first render also
            # exposes a positional drift; position is corrected on the next round.
            worsened = not size_improved and object_error(now) - object_error(before) > 0.5
            status_regressed = before.get("status") == "ok" and now.get("status") == "off"
            if (worsened or status_regressed) and not size_improved:
                if current is not None:
                    kind = current.get("type")
                    current.clear()
                    current.update(json.loads(json.dumps(item["object"])))
                    strikes[key] = int(strikes.get(key, 0)) + 1
                    rolled.append(key)
                    if strikes[key] >= 2:
                        frozen.add(key)
                        frozen.add(oid)
                        newly_frozen.append(key)
                    elif kind == "text" and size_changed:
                        limits[key] = {"position_only": True}
                    elif kind != "text":
                        limits[key] = {"step_scale": 0.5}
            pending.pop(key, None)
    return rolled, newly_frozen, limits


def absolutize_assets(payload: dict, base_dir: Path) -> dict:
    """Copy of payload with relative image paths made absolute, so the PageIR works from any folder."""
    out = json.loads(json.dumps(payload))
    for page in out.get("pages", []):
        for obj in page.get("objects", []):
            if obj.get("type") == "image" and isinstance(obj.get("path"), str) and not Path(obj["path"]).is_absolute():
                obj["path"] = str((base_dir / obj["path"]).resolve())
    return out


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
    frozen: set[str] = set()
    strikes: dict[str, int] = {}
    next_limits: dict[str, dict] = {}
    baseline_fonts = {
        str(obj.get("id")): float((obj.get("style") or {}).get("font_size_pt", 18))
        for page in payload.get("pages", [])
        for obj in page.get("objects", [])
        if obj.get("type") == "text"
    }
    pending: dict[str, dict] = {}
    try:
        for round_no in range(1, args.rounds + 1):
            pptx = workdir / f"round-{round_no}.pptx"
            compile_ir(payload, round_ir, pptx)
            report = run_compare(
                sources, pptx, workdir / f"round-{round_no}", payload, write_images=False, base_dir=ir_path.parent
            )
            current = score(report)
            rolled, newly_frozen, later_limits = apply_rollbacks(
                payload, report.get("slides", []), pending, strikes, frozen
            )
            # Limits from the previous rollback apply now, on a fresh measurement.
            # Objects restored this round are skipped: their fit belongs to the edit we just undid.
            limits = next_limits
            next_limits = later_limits
            history.append(
                {
                    "round": round_no,
                    "object_fit_ratio": report.get("object_fit_ratio"),
                    "mean_object_error_px": round(-current[1], 2),
                    "mean_mae": round(-current[2], 5),
                    "edge_f1": [round(s["edge_f1"], 4) for s in report["slides"]],
                }
            )
            if rolled:
                history[-1]["rollback"] = rolled
                history[-1]["strikes"] = {key: strikes[key] for key in rolled}
            if newly_frozen:
                history[-1]["frozen"] = newly_frozen
            if best is None or current > best[0]:
                best = (current, json.loads(json.dumps(payload)), pptx, report)
                stale = 0
            else:
                stale += 1
                if stale >= PATIENCE:
                    break  # corrections stopped helping
            if report.get("object_fit_ratio") == 1.0:
                break
            before_payload = json.loads(json.dumps(payload))
            payload, changed = refine_payload(
                payload, report["slides"], substituted_fonts(report), baseline_fonts, frozen, limits, set(rolled)
            )
            for key in changed:
                page_name, oid = key.split(":", 1)
                page = next((p for p in before_payload.get("pages", []) if str(p.get("id")) == page_name), None)
                if page:
                    old_obj = next((o for o in page.get("objects", []) if str(o.get("id")) == oid), None)
                    if old_obj is not None:
                        slide_index = next(i for i, p in enumerate(payload.get("pages", [])) if str(p.get("id")) == page_name)
                        old_fit = next(
                            (
                                f
                                for f in (report["slides"][slide_index].get("object_fit") or {}).get("objects", [])
                                if str(f.get("id")) == oid
                            ),
                            None,
                        )
                        if old_fit is not None:
                            new_obj = next(
                                (o for o in payload["pages"][slide_index].get("objects", []) if str(o.get("id")) == oid),
                                None,
                            )
                            pending[key] = {
                                "object": old_obj,
                                "fit": old_fit,
                                # Keep the edit classification explicit.  A
                                # same-round spacing/position edit can obscure
                                # the font delta when inferred from the live
                                # object during rollback.
                                "size_changed": (
                                    new_obj is not None
                                    and _font_sizes(new_obj) != _font_sizes(old_obj)
                                ),
                            }
            clamped_list = []
            suspect_list = []
            for s in report.get("slides", []):
                s_idx = int(s["slide"]) - 1
                pid = payload.get("pages", [])[s_idx].get("id", f"slide-{s['slide']}") if s_idx < len(payload.get("pages", [])) else f"slide-{s['slide']}"
                for f in (s.get("object_fit") or {}).get("objects", []):
                    if f.get("clamped"):
                        clamped_list.append(f"{pid}:{f['id']}")
                    if f.get("suspect"):
                        suspect_list.append(f"{pid}:{f['id']}")
            history[-1]["clamped"] = clamped_list
            history[-1]["suspect"] = suspect_list
            history[-1]["changed"] = changed
            if not changed:
                break
    finally:
        round_ir.unlink(missing_ok=True)

    assert best is not None
    _, best_payload, best_pptx, best_report = best
    best_ir = workdir / "page_ir.best.json"
    best_ir.write_text(
        json.dumps(absolutize_assets(best_payload, ir_path.parent), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    shutil.copy2(best_pptx, args.out)
    # Final artefacts (overlay, heatmap, metrics) for the delivered deck.
    final = run_compare(sources, Path(args.out), workdir / "final", best_payload, base_dir=ir_path.parent)
    (workdir / "final" / "metrics.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "output": str(Path(args.out).resolve()),
        "sources": [str(s) for s in sources],
        "best_page_ir": str(best_ir),
        "rounds": history,
        "object_fit_ratio": final.get("object_fit_ratio"),
        "slides": [
            {
                "slide": s["slide"],
                "normalized_mae": round(s["normalized_mae"], 4),
                "edge_f1": round(s["edge_f1"], 4),
                "font_hints": s.get("object_fit", {}).get("font_hints", []),
                "objects_off": [
                    f["id"] for f in s.get("object_fit", {}).get("objects", []) if f["status"] not in ("ok", "no_ink", "not_measured")
                ],
            }
            for s in final["slides"]
        ],
        "problems": final["problems"],
        "next": "Copy page_ir.best.json over your PageIR, then review final/overlay.png and the objects_off list.",
    }
    # Machine-readable record for delivery_gate.py.
    (workdir / "fidelity_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not final["problems"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
