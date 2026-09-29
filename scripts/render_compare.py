#!/usr/bin/env python3
"""Render a PPTX and compare each slide with its source image at source resolution.

Usage:
    render_compare.py source.png output.pptx --outdir quality
    render_compare.py page1.png page2.png output.pptx --outdir quality --page-ir page_ir.json

Source images are matched to slides in order. With one source image the outputs
are written directly into --outdir; with several, into --outdir/slide-N/.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


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
        for index, page in enumerate(pages, start=1):
            target = outdir / f"_rendered-{index}.png"
            shutil.copy2(page, target)
            rendered.append(target)
    return rendered


def edge_f1(a: np.ndarray, b: np.ndarray) -> float:
    ga = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    gb = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY)
    ea = cv2.Canny(ga, 80, 180) > 0
    eb = cv2.Canny(gb, 80, 180) > 0
    tp = np.logical_and(ea, eb).sum()
    fp = np.logical_and(~ea, eb).sum()
    fn = np.logical_and(ea, ~eb).sum()
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    return float(2 * precision * recall / max(precision + recall, 1e-12))


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
    smooth = cv2.GaussianBlur(diff_gray, (5, 5), 0).astype(np.float32) / 255.0
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


def compare_page(source_path: Path, rendered_path: Path, outdir: Path, objects: list[dict]) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    source = np.array(Image.open(source_path).convert("RGB"))
    rendered_img = Image.open(rendered_path).convert("RGB")
    source_aspect = source.shape[1] / source.shape[0]
    rendered_aspect = rendered_img.width / rendered_img.height
    aspect_error = abs(rendered_aspect - source_aspect) / source_aspect
    shutil.copy2(rendered_path, outdir / "rendered.png")

    rendered = np.array(rendered_img.resize((source.shape[1], source.shape[0]), Image.Resampling.LANCZOS))
    diff = cv2.absdiff(source, rendered)
    diff_gray = diff.max(axis=2)
    Image.fromarray(diff).save(outdir / "diff.png")
    Image.fromarray(rendered).save(outdir / "rendered_resized.png")
    overlay = cv2.addWeighted(source, 0.5, rendered, 0.5, 0)
    Image.fromarray(overlay).save(outdir / "overlay.png")
    heat = cv2.applyColorMap(cv2.GaussianBlur(diff_gray, (9, 9), 0), cv2.COLORMAP_JET)
    heat = cv2.addWeighted(cv2.cvtColor(source, cv2.COLOR_RGB2BGR), 0.4, heat, 0.6, 0)
    cv2.imwrite(str(outdir / "heatmap.png"), heat)

    return {
        "source": str(source_path),
        "source_width": int(source.shape[1]),
        "source_height": int(source.shape[0]),
        "rendered_width": int(rendered_img.width),
        "rendered_height": int(rendered_img.height),
        "aspect_error": round(aspect_error, 5),
        "aspect_ok": aspect_error <= ASPECT_TOLERANCE,
        "normalized_mae": float(np.mean(diff) / 255.0),
        "edge_f1": edge_f1(source, rendered),
        "hotspots": hotspots(diff_gray, objects),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", help="source image(s) followed by the PPTX")
    parser.add_argument("--outdir", default="quality")
    parser.add_argument("--page-ir", help="PageIR used to map difference hotspots to object ids")
    args = parser.parse_args()
    if len(args.paths) < 2:
        parser.error("expected at least one source image and a PPTX")
    sources = [Path(p) for p in args.paths[:-1]]
    pptx_path = Path(args.paths[-1])
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    pages_ir: list[dict] = []
    if args.page_ir:
        pages_ir = json.loads(Path(args.page_ir).read_text(encoding="utf-8")).get("pages", [])

    # Render at least at source resolution so thin strokes survive.
    max_src_w = max(Image.open(p).width for p in sources)
    dpi = int(min(300, max(96, math.ceil(max_src_w / slide_width_inches(pptx_path)))))
    rendered = render_slides(pptx_path, outdir, dpi)

    results = []
    problems = []
    if len(rendered) != len(sources):
        problems.append(f"{len(sources)} source image(s) but {len(rendered)} rendered slide(s)")
    for index, (source, rendered_path) in enumerate(zip(sources, rendered), start=1):
        page_dir = outdir if len(sources) == 1 else outdir / f"slide-{index}"
        objects = pages_ir[index - 1].get("objects", []) if index - 1 < len(pages_ir) else []
        result = {"slide": index, **compare_page(source, rendered_path, page_dir, objects)}
        if not result["aspect_ok"]:
            problems.append(f"slide {index} aspect ratio differs from source by {result['aspect_error']:.2%}")
        results.append(result)
    for path in rendered:
        path.unlink(missing_ok=True)

    report = {
        "pptx": str(pptx_path),
        "render_dpi": dpi,
        "slides": results,
        "problems": problems,
        "note": "MAE, edge F1 and hotspots are review signals; renderer and font differences change pixel scores.",
    }
    if len(results) == 1:
        # Keep the flat v0.1 keys for single-slide callers.
        report.update({k: results[0][k] for k in ("source_width", "source_height", "normalized_mae", "edge_f1")})
    (outdir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not problems else 2


if __name__ == "__main__":
    raise SystemExit(main())
