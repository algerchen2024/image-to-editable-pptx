#!/usr/bin/env python3
"""Render a PPTX and compare each slide with its source image at source resolution.

Usage:
    render_compare.py source.png output.pptx --outdir quality --page-ir page_ir.json
    render_compare.py page1.png page2.png output.pptx --outdir quality --page-ir page_ir.json

Source images are matched to slides in order. With one source image the outputs
are written directly into --outdir; with several, into --outdir/slide-N/.

Needs LibreOffice (soffice) and Poppler (pdftoppm) plus numpy, Pillow and
python-pptx. OpenCV is not required.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_ops  # noqa: E402
from font_check import font_report  # noqa: E402
from object_fit import measure_page, substituted_fonts  # noqa: E402
from compile_page_ir import compile_payload  # noqa: E402
from render_fonts import substitute_payload, substitutions_for_payload  # noqa: E402


ASPECT_TOLERANCE = 0.01
HOTSPOT_GRID = 24
HOTSPOT_COUNT = 8


def executable(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(f"Missing executable: {' or '.join(names)}")


def slide_width_inches(pptx: Path) -> float:
    from pptx import Presentation

    return int(Presentation(pptx).slide_width) / 914400.0


def render_slides(pptx: Path, outdir: Path, dpi: int) -> list[Path]:
    """Render every slide to outdir/_rendered-N.png and return the paths."""
    soffice = executable("soffice", "libreoffice")
    pdftoppm = executable("pdftoppm")
    rendered: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="pptx-render-") as tmp:
        tmp_path = Path(tmp)
        proc = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(pptx)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        pdf = tmp_path / f"{pptx.stem}.pdf"
        if proc.returncode != 0 or not pdf.exists():
            raise RuntimeError(f"LibreOffice export failed: {proc.stdout}")
        prefix = tmp_path / "slide"
        proc = subprocess.run(
            [pdftoppm, "-png", "-r", str(dpi), str(pdf), str(prefix)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=180,
        )
        pages = sorted(tmp_path.glob("slide-*.png"), key=lambda p: int(re.search(r"-(\d+)\.png$", p.name).group(1)))
        if proc.returncode != 0 or not pages:
            raise RuntimeError(f"pdftoppm failed: {proc.stdout}")
        outdir.mkdir(parents=True, exist_ok=True)
        for index, page in enumerate(pages, start=1):
            target = outdir / f"_rendered-{index}.png"
            shutil.copy2(page, target)
            rendered.append(target)
    return rendered


def object_box(obj: dict) -> tuple[float, float, float, float] | None:
    if obj.get("type") == "line" and isinstance(obj.get("points"), list) and len(obj["points"]) == 4:
        x1, y1, x2, y2 = [float(v) for v in obj["points"]]
        return min(x1, x2) - 2, min(y1, y2) - 2, abs(x2 - x1) + 4, abs(y2 - y1) + 4
    bbox = obj.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        return tuple(float(v) for v in bbox)  # type: ignore[return-value]
    return None


def objects_in_region(objects: list[dict], region: list[int]) -> list[str]:
    rx, ry, rw, rh = region
    hits = []
    for obj in objects:
        box = object_box(obj)
        if box is None:
            continue
        x, y, w, h = box
        if x < rx + rw and rx < x + w and y < ry + rh and ry < y + h:
            hits.append(str(obj.get("id")))
    return hits


def hotspots(diff_gray: np.ndarray, objects: list[dict]) -> list[dict]:
    """Largest-difference grid cells, with the PageIR objects that own them."""
    height, width = diff_gray.shape
    cell = max(8, math.ceil(max(width, height) / HOTSPOT_GRID))
    # Blur first so one-pixel anti-aliasing noise does not dominate.
    smooth = image_ops.blur(diff_gray, 2.0) / 255.0
    cells = []
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            block = smooth[y : y + cell, x : x + cell]
            cells.append((float(block.mean()), [x, y, min(cell, width - x), min(cell, height - y)]))
    cells.sort(key=lambda item: item[0], reverse=True)
    result = []
    for score, region in cells[:HOTSPOT_COUNT]:
        if score < 0.02:
            break
        entry = {"bbox": region, "mean_diff": round(score, 4)}
        if objects:
            entry["objects"] = objects_in_region(objects, region)
        result.append(entry)
    return result


def compare_images(
    source: np.ndarray,
    rendered: np.ndarray,
    objects: list[dict],
    outdir: Path | None,
    substituted: set[str] | None = None,
    default_font: str = "Arial",
) -> dict:
    """Compare a source image with a rendered slide already resized to source size."""
    diff = np.abs(source.astype(np.int16) - rendered.astype(np.int16)).astype(np.uint8)
    diff_gray = diff.max(axis=2)
    if outdir is not None:
        outdir.mkdir(parents=True, exist_ok=True)
        Image.fromarray(diff).save(outdir / "diff.png")
        Image.fromarray(rendered).save(outdir / "rendered_resized.png")
        Image.blend(Image.fromarray(source), Image.fromarray(rendered), 0.5).save(outdir / "overlay.png")
        heat = image_ops.heat_colors(image_ops.blur(diff_gray, 3.0))
        Image.blend(Image.fromarray(source), Image.fromarray(heat), 0.6).save(outdir / "heatmap.png")
    report = {
        "normalized_mae": float(np.mean(diff) / 255.0),
        "edge_f1": image_ops.edge_f1(source, rendered),
        "hotspots": hotspots(diff_gray, objects),
    }
    if objects:
        report["object_fit"] = measure_page(objects, source, rendered, substituted, default_font)
    return report


def compare_page(
    source_path: Path,
    rendered_path: Path,
    outdir: Path | None,
    objects: list[dict],
    substituted: set[str] | None = None,
    default_font: str = "Arial",
) -> dict:
    source = np.array(Image.open(source_path).convert("RGB"))
    rendered_img = Image.open(rendered_path).convert("RGB")
    source_aspect = source.shape[1] / source.shape[0]
    rendered_aspect = rendered_img.width / rendered_img.height
    aspect_error = abs(rendered_aspect - source_aspect) / source_aspect
    if outdir is not None:
        outdir.mkdir(parents=True, exist_ok=True)
        rendered_img.save(outdir / "rendered.png")
    rendered = np.array(rendered_img.resize((source.shape[1], source.shape[0]), Image.Resampling.LANCZOS))
    return {
        "source": str(source_path),
        "source_width": int(source.shape[1]),
        "source_height": int(source.shape[0]),
        "rendered_width": int(rendered_img.width),
        "rendered_height": int(rendered_img.height),
        "aspect_error": round(aspect_error, 5),
        "aspect_ok": aspect_error <= ASPECT_TOLERANCE,
        **compare_images(source, rendered, objects, outdir, substituted, default_font),
    }


def run_compare(
    sources: list[Path],
    pptx_path: Path,
    outdir: Path,
    payload: dict | None = None,
    write_images: bool = True,
    base_dir: Path | None = None,
) -> dict:
    """Render `pptx_path`, compare each slide with its source and return the report."""
    outdir.mkdir(parents=True, exist_ok=True)
    pages_ir = (payload or {}).get("pages", [])
    # Render at least at source resolution so thin strokes survive.
    with Image.open(sources[0]) as first_source:
        max_src_w = first_source.width
    for source_path in sources[1:]:
        with Image.open(source_path) as source_image:
            max_src_w = max(max_src_w, source_image.width)
    dpi = int(min(300, max(96, math.ceil(max_src_w / slide_width_inches(pptx_path)))))
    render_pptx = pptx_path
    render_copy_dir = None
    fonts = None
    render_copy_error = None
    try:
        if payload is not None:
            render_payload, substitutions = substitute_payload(payload)
            render_copy_dir = tempfile.TemporaryDirectory(prefix="pptx-render-copy-")
            render_copy_path = Path(render_copy_dir.name) / "render-copy.pptx"
            resolved_base_dir = base_dir.resolve() if base_dir is not None else Path.cwd()
            relative_assets = [
                Path(obj["path"])
                for page in payload.get("pages", [])
                for obj in page.get("objects", [])
                if obj.get("type") == "image" and isinstance(obj.get("path"), str) and not Path(obj["path"]).is_absolute()
            ]
            if relative_assets and base_dir is None:
                candidates = [resolved_base_dir, pptx_path.parent, *[s.parent for s in sources], *(s.parent.parent for s in sources)]
                for candidate in candidates:
                    if all((candidate / asset).exists() for asset in relative_assets):
                        resolved_base_dir = candidate
                        break
            render_copy_error = None
            try:
                compile_payload(render_payload, resolved_base_dir, render_copy_path)
                render_pptx = render_copy_path
            except Exception as exc:
                # Keep measuring the delivery deck, but do not hide the failure.
                render_copy_error = f"render copy compile failed: {exc}"
                render_pptx = pptx_path
            fonts = {
                **(font_report(payload) if payload is not None else {}),
                "substitutions": substitutions,
                "render_drawable": sorted(
                    {x.get("rendered_with") for x in substitutions if x.get("rendered_with")}
                ),
            }
            substituted = {
                x["requested"]
                for x in substitutions
                if x.get("substituted") and not x.get("metric_compatible")
            }
        else:
            substituted = set()
        rendered = render_slides(render_pptx, outdir, dpi)
        default_font = (payload or {}).get("default_font_face", "Arial")
        results = []
        problems = []
        if render_copy_error:
            problems.append(render_copy_error)
        if len(rendered) != len(sources):
            problems.append(f"{len(sources)} source image(s) but {len(rendered)} rendered slide(s)")
        for index, (source, rendered_path) in enumerate(zip(sources, rendered), start=1):
            page_dir = None
            if write_images:
                page_dir = outdir if len(sources) == 1 else outdir / f"slide-{index}"
            objects = pages_ir[index - 1].get("objects", []) if index - 1 < len(pages_ir) else []
            result = {"slide": index, **compare_page(source, rendered_path, page_dir, objects, substituted, default_font)}
            if not result["aspect_ok"]:
                problems.append(f"slide {index} aspect ratio differs from source by {result['aspect_error']:.2%}")
            results.append(result)
        for path in rendered:
            path.unlink(missing_ok=True)

        report: dict = {"pptx": str(pptx_path), "render_pptx": str(render_pptx), "render_dpi": dpi, "slides": results}
        if payload is not None:
            report["fonts"] = fonts
            problems += fonts["problems"]
            fits = [r["object_fit"] for r in results if "object_fit" in r]
            measured = sum(f["measured"] for f in fits)
            within = sum(f["within_tolerance"] for f in fits)
            report["object_fit_ratio"] = round(within / measured, 4) if measured else None
        report["problems"] = problems
        report["note"] = "MAE, edge F1 and hotspots are review signals; renderer and font differences change pixel scores."
        if len(results) == 1:
            # Keep the flat v0.1 keys for single-slide callers.
            report.update({k: results[0][k] for k in ("source_width", "source_height", "normalized_mae", "edge_f1")})
        return report
    finally:
        if render_copy_dir is not None:
            render_copy_dir.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", help="source image(s) followed by the PPTX")
    parser.add_argument("--outdir", default="quality")
    parser.add_argument("--page-ir", help="PageIR used for per-object fit, hotspot ownership and font checks")
    args = parser.parse_args()
    if len(args.paths) < 2:
        parser.error("expected at least one source image and a PPTX")
    payload = json.loads(Path(args.page_ir).read_text(encoding="utf-8")) if args.page_ir else None
    report = run_compare([Path(p) for p in args.paths[:-1]], Path(args.paths[-1]), Path(args.outdir), payload)
    outdir = Path(args.outdir)
    (outdir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "metrics": str(outdir / "metrics.json"),
        "slides": [
            {
                "slide": s["slide"],
                "normalized_mae": round(s["normalized_mae"], 4),
                "edge_f1": round(s["edge_f1"], 4),
                **(
                    {
                        "object_fit": f"{s['object_fit']['within_tolerance']}/{s['object_fit']['measured']}",
                        "worst_objects": s["object_fit"]["worst"],
                        "font_hints": s["object_fit"]["font_hints"],
                    }
                    if "object_fit" in s
                    else {}
                ),
                "hotspots": len(s["hotspots"]),
            }
            for s in report["slides"]
        ],
        "object_fit_ratio": report.get("object_fit_ratio"),
        "problems": report["problems"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not report["problems"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
