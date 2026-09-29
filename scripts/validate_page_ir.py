#!/usr/bin/env python3
"""Validate the compact PageIR used by the public image-to-editable-PPTX skill."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pageir_common import is_hex_color  # noqa: E402


SCHEMA_VERSIONS = {"1.0", "1.1"}
ALLOWED_TYPES = {"text", "rect", "round_rect", "ellipse", "triangle", "chevron", "line", "image"}
SHAPE_TYPES = {"rect", "round_rect", "ellipse", "triangle", "chevron"}
ALLOWED_ALIGN = {"left", "center", "right"}
ALLOWED_VALIGN = {"top", "mid", "bottom"}
ALLOWED_DASH = {"solid", "dash", "dot", "dash_dot"}
ALLOWED_ARROW = {"none", "triangle"}
IMAGE_ASPECT_TOLERANCE = 0.02


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_bbox(bbox: Any, page_w: float, page_h: float, path: str, errors: list[str]) -> None:
    if not isinstance(bbox, list) or len(bbox) != 4 or not all(is_number(v) for v in bbox):
        errors.append(f"{path}.bbox must be [x,y,width,height]")
        return
    x, y, w, h = [float(v) for v in bbox]
    if w <= 0 or h <= 0:
        errors.append(f"{path}.bbox width/height must be positive")
    tolerance = 1.0
    if x < -tolerance or y < -tolerance or x + w > page_w + tolerance or y + h > page_h + tolerance:
        errors.append(f"{path}.bbox is outside the page")


def check_color(style: dict, key: str, path: str, errors: list[str], nullable: bool = False) -> None:
    if key not in style:
        return
    value = style[key]
    if value is None and nullable:
        return
    if not is_hex_color(value):
        errors.append(f"{path}.style.{key} must be a #RGB/#RRGGBB hex color" + (" or null" if nullable else ""))


def check_positive(style: dict, key: str, path: str, errors: list[str], allow_zero: bool = False) -> None:
    if key not in style:
        return
    value = style[key]
    if not is_number(value) or float(value) < 0 or (not allow_zero and float(value) == 0):
        errors.append(f"{path}.style.{key} must be a {'non-negative' if allow_zero else 'positive'} number")


def validate_run_style(style: dict, path: str, errors: list[str]) -> None:
    check_color(style, "color", path, errors)
    check_positive(style, "font_size_pt", path, errors)
    check_positive(style, "line_spacing_multiple", path, errors)
    check_positive(style, "margin_pt", path, errors, allow_zero=True)
    if "char_spacing_pt" in style and not is_number(style["char_spacing_pt"]):
        errors.append(f"{path}.style.char_spacing_pt must be a number")
    if "font_face" in style and (not isinstance(style["font_face"], str) or not style["font_face"].strip()):
        errors.append(f"{path}.style.font_face must be a non-empty string")


def validate_text(obj: dict, opath: str, errors: list[str]) -> None:
    runs = obj.get("runs")
    if runs is not None:
        if not isinstance(runs, list) or not runs:
            errors.append(f"{opath}.runs must be a non-empty array")
        else:
            for r_idx, run in enumerate(runs):
                rpath = f"{opath}.runs[{r_idx}]"
                if not isinstance(run, dict) or not isinstance(run.get("text"), str):
                    errors.append(f"{rpath}.text must be a string")
                    continue
                validate_run_style(run, rpath, errors)
    elif not isinstance(obj.get("text"), str):
        errors.append(f"{opath}.text must be a string (or provide runs)")
    style = obj.get("style", {})
    if not isinstance(style, dict):
        errors.append(f"{opath}.style must be an object")
        return
    if style.get("align", "left") not in ALLOWED_ALIGN:
        errors.append(f"{opath}.style.align is invalid")
    if style.get("valign", "top") not in ALLOWED_VALIGN:
        errors.append(f"{opath}.style.valign is invalid")
    validate_run_style(style, opath, errors)


def validate_shape(obj: dict, opath: str, errors: list[str]) -> None:
    style = obj.get("style", {})
    if not isinstance(style, dict):
        errors.append(f"{opath}.style must be an object")
        return
    check_color(style, "fill", opath, errors, nullable=True)
    check_color(style, "line", opath, errors, nullable=True)
    check_positive(style, "line_width_pt", opath, errors, allow_zero=True)
    if style.get("line_dash", "solid") not in ALLOWED_DASH:
        errors.append(f"{opath}.style.line_dash is invalid")
    if "fill_transparency" in style:
        value = style["fill_transparency"]
        if not is_number(value) or not 0 <= float(value) <= 100:
            errors.append(f"{opath}.style.fill_transparency must be between 0 and 100")
    if "corner_radius_px" in style:
        if obj.get("type") != "round_rect":
            errors.append(f"{opath}.style.corner_radius_px is only valid for round_rect")
        else:
            check_positive(style, "corner_radius_px", opath, errors, allow_zero=True)


def validate_line(obj: dict, opath: str, page_w: float, page_h: float, errors: list[str]) -> None:
    points = obj.get("points")
    if not isinstance(points, list) or len(points) != 4 or not all(is_number(v) for v in points):
        errors.append(f"{opath}.points must be [x1,y1,x2,y2]")
    else:
        x1, y1, x2, y2 = [float(v) for v in points]
        if min(x1, x2) < -1 or min(y1, y2) < -1 or max(x1, x2) > page_w + 1 or max(y1, y2) > page_h + 1:
            errors.append(f"{opath}.points are outside the page")
        if x1 == x2 and y1 == y2:
            errors.append(f"{opath}.points describe a zero-length line")
    style = obj.get("style", {})
    if not isinstance(style, dict):
        errors.append(f"{opath}.style must be an object")
        return
    check_color(style, "color", opath, errors)
    check_positive(style, "width_pt", opath, errors)
    if style.get("dash", "solid") not in ALLOWED_DASH:
        errors.append(f"{opath}.style.dash is invalid")
    if style.get("start_arrow", "none") not in ALLOWED_ARROW or style.get("end_arrow", "none") not in ALLOWED_ARROW:
        errors.append(f"{opath}.style arrow type is invalid")


def image_aspect_warning(obj: dict, image_path: Path, opath: str) -> str | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    bbox = obj.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4 or not all(is_number(v) for v in bbox) or bbox[2] <= 0 or bbox[3] <= 0:
        return None
    try:
        with Image.open(image_path) as image:
            width, height = image.size
    except OSError:
        return f"{opath}.path could not be opened as an image"
    asset_ratio = width / height
    frame_ratio = float(bbox[2]) / float(bbox[3])
    if abs(asset_ratio - frame_ratio) / frame_ratio > IMAGE_ASPECT_TOLERANCE:
        return (
            f"{opath} image aspect {asset_ratio:.3f} differs from bbox aspect {frame_ratio:.3f}; "
            "the asset will be stretched"
        )
    return None


def validate_page_ir_detailed(payload: Any, base_dir: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Warnings do not fail validation."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return ["PageIR root must be an object"], warnings
    if payload.get("schema_version") not in SCHEMA_VERSIONS:
        errors.append(f"schema_version must be one of {sorted(SCHEMA_VERSIONS)}")
    pages = payload.get("pages")
    if not isinstance(pages, list) or not pages:
        errors.append("pages must be a non-empty array")
        return errors, warnings

    ratios: list[float] = []
    page_ids: set[str] = set()
    for p_idx, page in enumerate(pages):
        ppath = f"pages[{p_idx}]"
        if not isinstance(page, dict):
            errors.append(f"{ppath} must be an object")
            continue
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id.strip():
            errors.append(f"{ppath}.id must be a non-empty string")
        elif page_id in page_ids:
            errors.append(f"duplicate page id: {page_id}")
        else:
            page_ids.add(page_id)

        page_w = page.get("width_px")
        page_h = page.get("height_px")
        if not is_number(page_w) or float(page_w) <= 0 or not is_number(page_h) or float(page_h) <= 0:
            errors.append(f"{ppath}.width_px and height_px must be positive numbers")
            continue
        page_w = float(page_w)
        page_h = float(page_h)
        ratios.append(page_w / page_h)

        if not is_hex_color(page.get("background", "#FFFFFF")):
            errors.append(f"{ppath}.background must be a hex color")

        objects = page.get("objects")
        if not isinstance(objects, list):
            errors.append(f"{ppath}.objects must be an array")
            continue
        object_ids: set[str] = set()
        for o_idx, obj in enumerate(objects):
            opath = f"{ppath}.objects[{o_idx}]"
            if not isinstance(obj, dict):
                errors.append(f"{opath} must be an object")
                continue
            obj_id = obj.get("id")
            if not isinstance(obj_id, str) or not obj_id.strip():
                errors.append(f"{opath}.id must be a non-empty string")
            elif obj_id in object_ids:
                errors.append(f"duplicate object id on {page_id}: {obj_id}")
            else:
                object_ids.add(obj_id)
            obj_type = obj.get("type")
            if obj_type not in ALLOWED_TYPES:
                errors.append(f"{opath}.type must be one of {sorted(ALLOWED_TYPES)}")
                continue
            if "z" in obj and not is_number(obj["z"]):
                errors.append(f"{opath}.z must be a number")
            if "rotation_deg" in obj and not is_number(obj["rotation_deg"]):
                errors.append(f"{opath}.rotation_deg must be a number")

            if obj_type == "line":
                validate_line(obj, opath, page_w, page_h, errors)
                continue

            validate_bbox(obj.get("bbox"), page_w, page_h, opath, errors)

            if obj_type == "text":
                validate_text(obj, opath, errors)
            elif obj_type in SHAPE_TYPES:
                validate_shape(obj, opath, errors)
            elif obj_type == "image":
                rel = obj.get("path")
                if not isinstance(rel, str) or not rel.strip():
                    errors.append(f"{opath}.path must be a non-empty string")
                else:
                    image_path = (base_dir / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
                    if not image_path.exists():
                        errors.append(f"{opath}.path does not exist: {rel}")
                    else:
                        warning = image_aspect_warning(obj, image_path, opath)
                        if warning:
                            warnings.append(warning)

    if ratios:
        base = ratios[0]
        for idx, ratio in enumerate(ratios[1:], start=1):
            if abs(ratio - base) / base > 0.001:
                errors.append(f"pages[{idx}] aspect ratio differs by more than 0.1%")
    return errors, warnings


def validate_page_ir(payload: Any, base_dir: Path) -> list[str]:
    return validate_page_ir_detailed(payload, base_dir)[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("page_ir")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    path = Path(args.page_ir)
    payload = json.loads(path.read_text(encoding="utf-8"))
    errors, warnings = validate_page_ir_detailed(payload, path.resolve().parent)
    result = {"valid": not errors, "errors": errors, "warnings": warnings}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if errors:
            print("PageIR validation failed:")
            for item in errors:
                print(f"- {item}")
        else:
            print("PageIR validation passed")
        for item in warnings:
            print(f"WARNING: {item}")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
