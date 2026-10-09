#!/usr/bin/env python3
"""Per-object fidelity measurement and correction.

For every PageIR object, find the visible ink of that object in the source
image and in the rendered slide (same source-pixel region) and compare them:

- shapes, lines, images: the four ink edges;
- text: line by line. The first line's glyph height drives font size, the
  line count drives wrapping, the line pitch drives line spacing, the line
  width drives fine font size / letter spacing, and the first line's anchor
  drives position.

`refine_pageir.py` applies the corrections; `fidelity_loop.py` repeats
compile -> render -> measure -> refine until objects match.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_ops import ink_box, ink_lines  # noqa: E402
from pageir_common import pt_per_px  # noqa: E402


SIZE_TOLERANCE = 0.04  # glyph height
WIDTH_TOLERANCE = 0.015  # line width
PITCH_TOLERANCE = 0.03  # line spacing
FONT_FINE_TUNE_LIMIT = 0.06
MAX_PAGE_COVERAGE = 0.6
FONT_SCALE_LIMITS = (0.8, 1.25)
CHAR_SPACING_LIMITS = (-3.0, 6.0)
LINE_SPACING_LIMITS = (0.7, 2.5)
GROW_STEPS = (1.0, 2.0, 4.0)


def position_tolerance(page_w: float) -> float:
    """Allowed ink displacement in source pixels: 2 px or 0.2% of the page width."""
    return max(2.0, 0.002 * page_w)


def text_of(obj: dict) -> str:
    if isinstance(obj.get("runs"), list):
        return "".join(str(r.get("text", "")) for r in obj["runs"])
    return str(obj.get("text") or "")


def object_region(obj: dict, page_w: float, page_h: float, grow: float = 1.0):
    """Search region [x0, y0, x1, y1] around an object, or None when it cannot be measured.

    `grow` widens the margin; measure_object retries with larger margins when an
    object has drifted out of, or is clipped by, the default search window.
    """
    if obj.get("rotation_deg"):
        return None
    if obj.get("type") == "line":
        pts = obj.get("points")
        if not isinstance(pts, list) or len(pts) != 4:
            return None
        x1, y1, x2, y2 = (float(v) for v in pts)
        pad = 12.0 * grow
        return [min(x1, x2) - pad, min(y1, y2) - pad, max(x1, x2) + pad, max(y1, y2) + pad]
    bbox = obj.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    x, y, w, h = (float(v) for v in bbox)
    if w * h > MAX_PAGE_COVERAGE * page_w * page_h:
        return None  # background-sized panels have no surrounding border to measure against
    if obj.get("type") == "text":
        # Margin from the font size, not the box: tall multi-line boxes would
        # otherwise reach out to the outline of an enclosing card.
        font_pt = float((obj.get("style") or {}).get("font_size_pt", 18))
        pad = max(4.0, 0.5 * font_pt / pt_per_px(page_w, page_h)) * grow
    else:
        pad = max(6.0, 0.05 * min(w, h)) * grow
    return [x - pad, y - pad, x + w + pad, y + h + pad]


def _touches(box, region, page_w: float, page_h: float) -> bool:
    """True when ink reaches the search window edge (and the window is not at the page edge)."""
    rx0, ry0 = max(0.0, region[0]), max(0.0, region[1])
    rx1, ry1 = min(page_w, region[2]), min(page_h, region[3])
    return (
        (box[0] <= rx0 + 1 and rx0 > 0)
        or (box[1] <= ry0 + 1 and ry0 > 0)
        or (box[2] >= rx1 - 1 and rx1 < page_w)
        or (box[3] >= ry1 - 1 and ry1 < page_h)
    )


def _measure_box(obj, source, rendered, page_w, page_h):
    src = ren = None
    for grow in GROW_STEPS:
        region = object_region(obj, page_w, page_h, grow)
        src = ink_box(source, region)
        ren = ink_box(rendered, region)
        if src is not None and ren is not None and not (
            _touches(src, region, page_w, page_h) or _touches(ren, region, page_w, page_h)
        ):
            break
    return src, ren


def _measure_lines(obj, source, rendered, page_w, page_h):
    src = ren = []
    for grow in GROW_STEPS:
        region = object_region(obj, page_w, page_h, grow)
        src = ink_lines(source, region)
        ren = ink_lines(rendered, region)
        if src and ren:
            break
    return src, ren


def _union(lines):
    return [min(b[0] for b in lines), min(b[1] for b in lines), max(b[2] for b in lines), max(b[3] for b in lines)]


def measure_text(obj: dict, source, rendered, page_w: float, page_h: float, result: dict) -> dict:
    src_lines, ren_lines = _measure_lines(obj, source, rendered, page_w, page_h)
    if not src_lines and not ren_lines:
        result["status"] = "no_ink"
        return result
    if not ren_lines:
        result.update(status="missing_in_render", source_ink=_union(src_lines))
        return result
    if not src_lines:
        result.update(status="extra_in_render", render_ink=_union(ren_lines))
        return result

    tol = position_tolerance(page_w)
    s0, r0 = src_lines[0], ren_lines[0]
    scale_h = (s0[3] - s0[1]) / max(r0[3] - r0[1], 1)
    align = (obj.get("style") or {}).get("align", "left")
    if align == "left":
        anchor_dx = s0[0] - r0[0]
    elif align == "right":
        anchor_dx = s0[2] - r0[2]
    else:
        anchor_dx = (s0[0] + s0[2] - r0[0] - r0[2]) / 2
    anchor_dy = (s0[1] + s0[3] - r0[1] - r0[3]) / 2
    # Widths are only comparable line-for-line when the source has no soft
    # wraps (each visual line is one logical line); compare the first line.
    logical_lines = text_of(obj).split("\n")
    same_lines = len(src_lines) == len(ren_lines)
    width_comparable = same_lines and len(src_lines) == len(logical_lines)
    src_w = s0[2] - s0[0]
    ren_w = r0[2] - r0[0]
    pitch_ratio = None
    if same_lines and len(src_lines) > 1:
        pitch_ratio = (src_lines[-1][1] - s0[1]) / max(ren_lines[-1][1] - r0[1], 1)
    result.update(
        {
            "source_ink": _union(src_lines),
            "render_ink": _union(ren_lines),
            "source_lines": len(src_lines),
            "render_lines": len(ren_lines),
            "first_line_source": s0,
            "first_line_render": r0,
            "anchor_dx": round(float(anchor_dx), 1),
            "anchor_dy": round(float(anchor_dy), 1),
            "scale_h": round(scale_h, 4),
            "source_width": int(src_w),
            "render_width": int(ren_w),
            "width_comparable": width_comparable,
            "pitch_ratio": round(pitch_ratio, 4) if pitch_ratio is not None else None,
        }
    )
    ok = (
        same_lines
        and abs(anchor_dx) <= tol
        and abs(anchor_dy) <= tol
        and abs(scale_h - 1) <= SIZE_TOLERANCE
        and (not width_comparable or abs(src_w - ren_w) <= max(2 * tol, WIDTH_TOLERANCE * src_w))
        and (pitch_ratio is None or abs(pitch_ratio - 1) <= PITCH_TOLERANCE)
    )
    result["status"] = "ok" if ok else "off"
    return result


def measure_object(obj: dict, source: np.ndarray, rendered: np.ndarray, page_w: float, page_h: float) -> dict:
    result: dict = {"id": obj.get("id"), "type": obj.get("type")}
    if object_region(obj, page_w, page_h) is None:
        result["status"] = "not_measured"
        return result
    if obj.get("type") == "text":
        return measure_text(obj, source, rendered, page_w, page_h, result)

    src, ren = _measure_box(obj, source, rendered, page_w, page_h)
    if src is None and ren is None:
        result["status"] = "no_ink"
        return result
    if src is None:
        result.update(status="extra_in_render", render_ink=ren)
        return result
    if ren is None:
        result.update(status="missing_in_render", source_ink=src)
        return result
    tol = position_tolerance(page_w)
    edges = {"dx0": src[0] - ren[0], "dy0": src[1] - ren[1], "dx1": src[2] - ren[2], "dy1": src[3] - ren[3]}
    result.update({"source_ink": src, "render_ink": ren, **edges})
    result["status"] = "ok" if all(abs(v) <= tol for v in edges.values()) else "off"
    return result


def _error(f: dict) -> float:
    keys = ("dx0", "dy0", "dx1", "dy1", "anchor_dx", "anchor_dy")
    err = max([abs(f.get(k) or 0) for k in keys] + [0])
    if f.get("scale_h"):
        err = max(err, abs(f["scale_h"] - 1) * 100)
    if f["status"] in ("missing_in_render", "extra_in_render"):
        err = float("inf")
    return err


def measure_page(objects: list[dict], source: np.ndarray, rendered: np.ndarray) -> dict:
    page_h, page_w = source.shape[:2]
    fits = [measure_object(obj, source, rendered, page_w, page_h) for obj in objects]
    measured = [f for f in fits if f["status"] in ("ok", "off", "missing_in_render", "extra_in_render")]
    ok = sum(1 for f in measured if f["status"] == "ok")
    worst = sorted((f for f in measured if f["status"] != "ok"), key=_error, reverse=True)
    return {
        "tolerance_px": round(position_tolerance(page_w), 2),
        "measured": len(measured),
        "within_tolerance": ok,
        "fit_ratio": round(ok / len(measured), 4) if measured else 1.0,
        "worst": [f["id"] for f in worst[:10]],
        "objects": fits,
    }


# ---------------------------------------------------------------- refinement


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _clamp_box(box: list[float], page_w: float, page_h: float) -> list[float]:
    x, y, w, h = box
    w = max(1.0, min(w, page_w))
    h = max(1.0, min(h, page_h))
    x = max(0.0, min(x, page_w - w))
    y = max(0.0, min(y, page_h - h))
    return [round(x, 1), round(y, 1), round(w, 1), round(h, 1)]


def _scale_fonts(obj: dict, factor: float) -> bool:
    style = obj.setdefault("style", {})
    base = float(style.get("font_size_pt", 18))
    new = max(4.0, round(base * factor * 2) / 2)
    if new == base:
        return False
    style["font_size_pt"] = new
    for run in obj.get("runs") or []:
        if "font_size_pt" in run:
            run["font_size_pt"] = max(4.0, round(float(run["font_size_pt"]) * factor * 2) / 2)
    return True


def _shape_text(obj: dict, fit: dict, page_w: float, page_h: float, tune_width: bool = True) -> bool:
    """Size, wrapping, width and spacing: one correction per round, in dependency order."""
    style = obj.setdefault("style", {})
    x, y, w, h = (float(v) for v in obj["bbox"])
    tol = position_tolerance(page_w)
    text = text_of(obj)

    # 1. Font size from the first line's glyph height.
    scale_h = fit["scale_h"]
    if abs(scale_h - 1) > SIZE_TOLERANCE:
        if _scale_fonts(obj, min(max(scale_h, FONT_SCALE_LIMITS[0]), FONT_SCALE_LIMITS[1])):
            return True

    # 2. Line count (wrapping).
    src_n, ren_n = fit["source_lines"], fit["render_lines"]
    if ren_n > src_n:
        if src_n == 1:
            style["wrap"] = False
            w = max(w, fit["source_width"] * 1.1)
        else:
            w *= 1.06
        obj["bbox"] = _clamp_box([x, y, w, h], page_w, page_h)
        return True
    if ren_n < src_n and "\n" not in text and style.get("wrap", True) is not False:
        obj["bbox"] = _clamp_box([x, y, w * 0.95, h], page_w, page_h)
        return True

    # 3. First-line width: fine font size first (glyph height cannot resolve
    #    half a point, width can), then letter spacing for substituted fonts.
    src_w, ren_w = fit["source_width"], fit["render_width"]
    n_chars = len(text.split("\n")[0])
    if tune_width and fit.get("width_comparable") and n_chars > 1 and abs(src_w - ren_w) > max(2 * tol, WIDTH_TOLERANCE * src_w):
        scale_w = src_w / max(ren_w, 1)
        if "char_spacing_pt" not in style and abs(scale_w - 1) <= FONT_FINE_TUNE_LIMIT and _scale_fonts(obj, scale_w):
            return True
        delta_pt = (src_w - ren_w) * pt_per_px(page_w, page_h) / (n_chars - 1)
        spacing = float(style.get("char_spacing_pt", 0)) + delta_pt
        style["char_spacing_pt"] = round(min(max(spacing, CHAR_SPACING_LIMITS[0]), CHAR_SPACING_LIMITS[1]), 2)
        return True

    # 4. Line spacing from the line pitch.
    pitch = fit.get("pitch_ratio")
    if pitch is not None and abs(pitch - 1) > PITCH_TOLERANCE:
        current = float(style.get("line_spacing_multiple", 1.0))
        style["line_spacing_multiple"] = round(min(max(current * pitch, LINE_SPACING_LIMITS[0]), LINE_SPACING_LIMITS[1]), 3)
        return True
    return False


def refine_text(obj: dict, fit: dict, page_w: float, page_h: float, tune_width: bool = True) -> bool:
    """tune_width=False when the render environment substitutes this object's font: line
    width then reflects the substitute's metrics, and matching it would make the text
    wrong in PowerPoint with the real font."""
    changed = _shape_text(obj, fit, page_w, page_h, tune_width)
    # Position every round: moving the box does not disturb size or wrapping,
    # and the next measurement absorbs any drift a size change causes.
    x, y, w, h = (float(v) for v in obj["bbox"])
    dx, dy = fit["anchor_dx"], fit["anchor_dy"]
    if abs(dx) > position_tolerance(page_w) / 2 or abs(dy) > position_tolerance(page_w) / 2:
        limit = max(8.0, 0.5 * h)
        new = _clamp_box([x + _clamp(dx, limit), y + _clamp(dy, limit), w, h], page_w, page_h)
        if new != obj["bbox"]:
            obj["bbox"] = new
            changed = True
    return changed


def refine_box(obj: dict, fit: dict, page_w: float, page_h: float) -> bool:
    x, y, w, h = (float(v) for v in obj["bbox"])
    limit_x = max(8.0, 0.25 * w)
    limit_y = max(8.0, 0.25 * h)
    x0 = x + _clamp(fit["dx0"], limit_x)
    x1 = x + w + _clamp(fit["dx1"], limit_x)
    y0 = y + _clamp(fit["dy0"], limit_y)
    y1 = y + h + _clamp(fit["dy1"], limit_y)
    if x1 - x0 < 1 or y1 - y0 < 1:
        return False
    new = _clamp_box([x0, y0, x1 - x0, y1 - y0], page_w, page_h)
    if new == _clamp_box([x, y, w, h], page_w, page_h):
        return False
    obj["bbox"] = new
    return True


def refine_line(obj: dict, fit: dict, page_w: float, page_h: float) -> bool:
    x1, y1, x2, y2 = (float(v) for v in obj["points"])
    # Per-round move limit scales with the line, as for boxes, so long lines converge quickly.
    limit = max(16.0, 0.25 * max(abs(x2 - x1), abs(y2 - y1)))

    def move(a: float, b: float, d0: float, d1: float) -> tuple[float, float]:
        if abs(a - b) < 1:  # degenerate axis (horizontal/vertical line): move by the centre shift
            d = (d0 + d1) / 2
            return a + _clamp(d, limit), b + _clamp(d, limit)
        lo, hi = (d0, d1) if a < b else (d1, d0)
        return a + _clamp(lo, limit), b + _clamp(hi, limit)

    nx1, nx2 = move(x1, x2, fit["dx0"], fit["dx1"])
    ny1, ny2 = move(y1, y2, fit["dy0"], fit["dy1"])

    def clamp(v: float, hi: float) -> float:
        return round(max(0.0, min(hi, v)), 1)

    new = [clamp(nx1, page_w), clamp(ny1, page_h), clamp(nx2, page_w), clamp(ny2, page_h)]
    if (new[0] == new[2] and new[1] == new[3]) or new == [round(v, 1) for v in (x1, y1, x2, y2)]:
        return False
    obj["points"] = new
    return True


def uses_font(obj: dict, fonts: set[str], default: str = "Arial") -> bool:
    names = {(obj.get("style") or {}).get("font_face") or default}
    names |= {r["font_face"] for r in obj.get("runs") or [] if r.get("font_face")}
    return bool({n.lower() for n in names} & {f.lower() for f in fonts})


def refine_page(page: dict, fits: list[dict], substituted_fonts: set[str] | None = None, default_font: str = "Arial") -> list[str]:
    """Apply measured corrections to one PageIR page in place. Returns changed object ids."""
    substituted_fonts = substituted_fonts or set()
    page_w = float(page["width_px"])
    page_h = float(page["height_px"])
    by_id = {str(f.get("id")): f for f in fits}
    changed = []
    for obj in page.get("objects", []):
        fit = by_id.get(str(obj.get("id")))
        if not fit or fit.get("status") != "off":
            continue
        if obj.get("type") == "text":
            did = refine_text(obj, fit, page_w, page_h, not uses_font(obj, substituted_fonts, default_font))
        elif obj.get("type") == "line":
            did = refine_line(obj, fit, page_w, page_h)
        else:
            did = refine_box(obj, fit, page_w, page_h)
        if did:
            changed.append(str(obj.get("id")))
    return changed


def substituted_fonts(report: dict) -> set[str]:
    """Fonts that render_compare found substituted in the render environment."""
    return {
        s["requested"]
        for s in (report.get("fonts") or {}).get("substitutions", [])
        if s.get("substituted") and not s.get("metric_compatible")
    }


def refine_payload(payload: dict, slide_reports: list[dict], substituted: set[str] | None = None) -> tuple[dict, list[str]]:
    refined = copy.deepcopy(payload)
    default_font = payload.get("default_font_face", "Arial")
    changed: list[str] = []
    for index, report in enumerate(slide_reports):
        if index >= len(refined.get("pages", [])):
            break
        fits = (report.get("object_fit") or {}).get("objects", [])
        page = refined["pages"][index]
        changed += [f"{page.get('id')}:{oid}" for oid in refine_page(page, fits, substituted, default_font)]
    return refined, changed
