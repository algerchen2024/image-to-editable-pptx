#!/usr/bin/env python3
"""Compile PageIR into an editable PPTX with python-pptx (no Node.js needed).

    compile_page_ir.py page_ir.json output.pptx

This is the default compiler: python-pptx is available in restricted sandboxes
(for example ChatGPT code execution) where Node packages cannot be installed.
It produces the same objects as compile_page_ir.js.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pageir_common import normalize_hex, slide_size_inches  # noqa: E402


EMU_PER_INCH = 914400
SHAPES = {
    "rect": MSO_SHAPE.RECTANGLE,
    "round_rect": MSO_SHAPE.ROUNDED_RECTANGLE,
    "ellipse": MSO_SHAPE.OVAL,
    "triangle": MSO_SHAPE.ISOSCELES_TRIANGLE,
    "chevron": MSO_SHAPE.CHEVRON,
}
DASH = {"solid": "solid", "dash": "dash", "dot": "sysDot", "dash_dot": "dashDot"}
ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
VALIGN = {"top": MSO_ANCHOR.TOP, "mid": MSO_ANCHOR.MIDDLE, "bottom": MSO_ANCHOR.BOTTOM}


def rgb(value, fallback: str) -> RGBColor:
    return RGBColor.from_string(normalize_hex(value if value else fallback)[1:])


def _sub(parent, tag: str, **attrs):
    el = etree.SubElement(parent, qn(tag))
    for key, val in attrs.items():
        el.set(key, str(val))
    return el


def set_line(shape_line, color, width_pt: float, dash: str | None) -> None:
    shape_line.color.rgb = rgb(color, "#111111")
    shape_line.width = Pt(width_pt)
    ln = shape_line._get_or_add_ln()
    for old in ln.findall(qn("a:prstDash")):
        ln.remove(old)
    prst = etree.Element(qn("a:prstDash"))
    prst.set("val", DASH.get(dash or "solid", "solid"))
    # prstDash must follow the fill element inside a:ln.
    fill = ln.find(qn("a:solidFill"))
    if fill is not None:
        fill.addnext(prst)
    else:
        ln.insert(0, prst)


def drop_theme_style(shape) -> None:
    """Remove python-pptx's theme style reference (it adds a drop shadow and theme
    colors); every fill, line and font is set explicitly instead."""
    style = shape._element.find(qn("p:style"))
    if style is not None:
        shape._element.remove(style)


def set_rotation(shape, deg) -> None:
    if deg:
        shape.rotation = float(deg)


def apply_run_style(run, style: dict, base: dict, lang: str) -> None:
    merged = {**base, **{k: v for k, v in style.items() if v is not None}}
    font = run.font
    font.size = Pt(float(merged.get("font_size_pt", 18)))
    font.bold = bool(merged.get("bold", False))
    font.italic = bool(merged.get("italic", False))
    font.underline = bool(merged.get("underline", False))
    font.color.rgb = rgb(merged.get("color"), "#111111")
    face = merged.get("font_face") or "Arial"
    rpr = run._r.get_or_add_rPr()
    rpr.set("lang", lang)
    if merged.get("char_spacing_pt") is not None:
        rpr.set("spc", str(int(round(float(merged["char_spacing_pt"]) * 100))))
    for tag in ("a:latin", "a:ea", "a:cs"):
        for old in rpr.findall(qn(tag)):
            rpr.remove(old)
    # Latin, East Asian and complex-script slots all get the face, as PptxGenJS does.
    for tag in ("a:latin", "a:ea", "a:cs"):
        _sub(rpr, tag, typeface=face)


def add_text(slide, obj: dict, box, lang: str) -> None:
    style = obj.get("style") or {}
    shape = slide.shapes.add_textbox(*box)
    tf = shape.text_frame
    tf.word_wrap = style.get("wrap", True) is not False
    tf.auto_size = MSO_AUTO_SIZE.NONE
    margin = Pt(float(style.get("margin_pt", 0)))
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = margin
    tf.vertical_anchor = VALIGN.get(style.get("valign", "top"), MSO_ANCHOR.TOP)
    base = {
        "font_face": style.get("font_face") or "Arial",
        "font_size_pt": style.get("font_size_pt", 18),
        "bold": style.get("bold", False),
        "italic": style.get("italic", False),
        "underline": style.get("underline", False),
        "color": style.get("color") or "#111111",
        "char_spacing_pt": style.get("char_spacing_pt"),
    }
    # Split runs into paragraphs at newlines.
    runs = obj.get("runs") if isinstance(obj.get("runs"), list) else [{"text": obj.get("text") or ""}]
    paragraphs: list[list[tuple[str, dict]]] = [[]]
    for run in runs:
        parts = str(run.get("text", "")).split("\n")
        for i, part in enumerate(parts):
            if i:
                paragraphs.append([])
            if part:
                paragraphs[-1].append((part, run))
    for p_index, items in enumerate(paragraphs):
        para = tf.paragraphs[0] if p_index == 0 else tf.add_paragraph()
        para.alignment = ALIGN.get(style.get("align", "left"), PP_ALIGN.LEFT)
        para.line_spacing = float(style.get("line_spacing_multiple", 1.0))
        para.space_before = Pt(0)
        para.space_after = Pt(0)
        if not items:
            # Keep empty lines at the right size.
            end = para._p.get_or_add_endParaRPr()
            end.set("sz", str(int(float(base["font_size_pt"]) * 100)))
        for text, run_style in items:
            run = para.add_run()
            run.text = text
            apply_run_style(run, {k: v for k, v in run_style.items() if k != "text"}, base, lang)
    set_rotation(shape, obj.get("rotation_deg"))


def add_shape(slide, obj: dict, box, scale: float) -> None:
    style = obj.get("style") or {}
    shape = slide.shapes.add_shape(SHAPES[obj["type"]], *box)
    if style.get("fill", "#FFFFFF") is None:
        shape.fill.background()
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = rgb(style.get("fill"), "#FFFFFF")
        transparency = float(style.get("fill_transparency", 0) or 0)
        if transparency:
            clr = shape.fill._xPr.find(qn("a:solidFill")).find(qn("a:srgbClr"))
            _sub(clr, "a:alpha", val=int(round((100 - transparency) * 1000)))
    if style.get("line", "#111111") is None:
        shape.line.fill.background()
    else:
        set_line(shape.line, style.get("line"), float(style.get("line_width_pt", 1)), style.get("line_dash"))
    if obj["type"] == "round_rect" and style.get("corner_radius_px") is not None:
        radius_emu = float(style["corner_radius_px"]) * scale
        shortest = max(1, min(box[2], box[3]))
        shape.adjustments[0] = max(0.0, min(0.5, radius_emu / shortest))
    drop_theme_style(shape)
    set_rotation(shape, obj.get("rotation_deg"))


def add_line(slide, obj: dict, sx: float, sy: float) -> None:
    style = obj.get("style") or {}
    x1, y1, x2, y2 = (float(v) for v in obj["points"])
    conn = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Emu(int(x1 * sx)), Emu(int(y1 * sy)), Emu(int(x2 * sx)), Emu(int(y2 * sy))
    )
    drop_theme_style(conn)
    set_line(conn.line, style.get("color"), float(style.get("width_pt", 1)), style.get("dash"))
    ln = conn.line._get_or_add_ln()
    for key, tag in (("start_arrow", "a:headEnd"), ("end_arrow", "a:tailEnd")):
        if style.get(key) == "triangle":
            _sub(ln, tag, type="triangle")


def add_image(slide, obj: dict, box, base_dir: Path) -> None:
    path = Path(obj["path"])
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    pic = slide.shapes.add_picture(str(path), *box)
    set_rotation(pic, obj.get("rotation_deg"))


def compile_payload(payload: dict, base_dir: Path, output: Path) -> dict:
    pages = payload.get("pages") or []
    if not pages:
        raise ValueError("PageIR contains no pages")
    first = pages[0]
    aspect = float(first["width_px"]) / float(first["height_px"])
    width_in, height_in = slide_size_inches(float(first["width_px"]), float(first["height_px"]))
    lang = payload.get("lang") or "zh-CN"

    prs = Presentation()
    prs.slide_width = Emu(int(round(width_in * EMU_PER_INCH)))
    prs.slide_height = Emu(int(round(height_in * EMU_PER_INCH)))
    prs.core_properties.title = payload.get("title") or "Editable PowerPoint reconstruction"
    prs.core_properties.author = "image-to-editable-pptx"
    blank = prs.slide_layouts[6]
    for page in pages:
        if abs(float(page["width_px"]) / float(page["height_px"]) - aspect) / aspect > 0.001:
            raise ValueError("All pages must share the same aspect ratio")
        slide = prs.slides.add_slide(blank)
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = rgb(page.get("background"), "#FFFFFF")
        sx = prs.slide_width / float(page["width_px"])
        sy = prs.slide_height / float(page["height_px"])
        page_lang = page.get("lang") or lang
        # Stable sort: equal z keeps PageIR order.
        for obj in sorted(page.get("objects") or [], key=lambda o: float(o.get("z", 0) or 0)):
            if obj["type"] == "line":
                add_line(slide, obj, sx, sy)
                continue
            x, y, w, h = (float(v) for v in obj["bbox"])
            box = (Emu(int(round(x * sx))), Emu(int(round(y * sy))), Emu(int(round(w * sx))), Emu(int(round(h * sy))))
            if obj["type"] == "text":
                add_text(slide, obj, box, page_lang)
            elif obj["type"] == "image":
                add_image(slide, obj, box, base_dir)
            else:
                add_shape(slide, obj, box, min(sx, sy))
    output.parent.mkdir(parents=True, exist_ok=True)
    prs.save(output)
    return {"output": str(output), "slides": len(pages), "width_in": width_in, "height_in": height_in}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("page_ir")
    parser.add_argument("output")
    args = parser.parse_args()
    ir_path = Path(args.page_ir).resolve()
    payload = json.loads(ir_path.read_text(encoding="utf-8"))
    try:
        result = compile_payload(copy.deepcopy(payload), ir_path.parent, Path(args.output).resolve())
    except (ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
