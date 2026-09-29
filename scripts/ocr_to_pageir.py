#!/usr/bin/env python3
"""Turn ocr_lines.py output into a draft PageIR with estimated font sizes and colors.

The draft is a starting point, not a result: every text object carries a
`note` with its OCR confidence, and wording, font, weight, and geometry must
still be reviewed against the source.

Example:
    ocr_to_pageir.py analysis/ocr_lines.json --image source.png --out page_ir.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pageir_common import pt_per_px  # noqa: E402


CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")
TALL_RE = re.compile(r"[A-Z0-9bdfhiklt()\[\]{}|/\\!?%&#@$]")
DESCENDER_RE = re.compile(r"[gjpqy,;()\[\]{}|]")
LOW_CONFIDENCE = 70.0


def ink_to_em_ratio(text: str) -> float:
    """Approximate height of a line's ink box as a fraction of the font's em size."""
    if CJK_RE.search(text):
        return 0.88
    ratio = 0.72 if TALL_RE.search(text) else 0.52
    if DESCENDER_RE.search(text):
        ratio += 0.22
    return ratio


def estimate_font_pt(text: str, ink_height_px: float, page_w: float, page_h: float) -> float:
    em_px = ink_height_px / ink_to_em_ratio(text)
    return max(6.0, round(em_px * pt_per_px(page_w, page_h) * 2) / 2)


def draft_text_object(line: dict, index: int, page_w: float, page_h: float, fonts: dict, image=None) -> dict:
    text = line["text"]
    x, y, w, h = (float(v) for v in line["bbox"])
    font_pt = estimate_font_pt(text, h, page_w, page_h)
    em_px = font_pt / pt_per_px(page_w, page_h)
    # PowerPoint lays a single line out in ~1.2 em; centre that box on the ink
    # and leave horizontal slack so the label cannot wrap.
    box_h = min(page_h, em_px * 1.2)
    box_y = min(max(0.0, y + h / 2 - box_h / 2), page_h - box_h)
    box_w = min(page_w - x, w + em_px * 0.5)
    style = {
        "font_face": fonts["cjk"] if CJK_RE.search(text) else fonts["latin"],
        "font_size_pt": font_pt,
        "bold": False,
        "color": "#111111",
        "align": "left",
        "valign": "mid",
        "margin_pt": 0,
        "wrap": False,
    }
    if image is not None:
        from sample_colors import sample_box

        sample = sample_box(image, [x, y, w, h])
        if sample["foreground"]:
            style["color"] = sample["foreground"]
    confidence = float(line.get("confidence", 0))
    note = f"ocr-draft confidence={confidence:.0f}"
    if confidence < LOW_CONFIDENCE:
        note += " LOW - verify wording"
    return {
        "id": f"text-{index}",
        "type": "text",
        "bbox": [round(x, 1), round(box_y, 1), round(max(1.0, box_w), 1), round(box_h, 1)],
        "text": text,
        "z": 10,
        "style": style,
        "note": note,
    }


def build_page_ir(ocr: dict, fonts: dict, image=None, background: str | None = None) -> dict:
    page_w = float(ocr["width_px"])
    page_h = float(ocr["height_px"])
    objects = [
        draft_text_object(line, index, page_w, page_h, fonts, image)
        for index, line in enumerate(ocr.get("lines", []), start=1)
        if str(line.get("text", "")).strip()
    ]
    if background is None and image is not None:
        from sample_colors import page_background

        background = page_background(image)
    return {
        "schema_version": "1.1",
        "pages": [
            {
                "id": "slide-1",
                "width_px": int(page_w),
                "height_px": int(page_h),
                "background": background or "#FFFFFF",
                "objects": objects,
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ocr_json", help="output of ocr_lines.py")
    parser.add_argument("--image", help="source image; enables sampled text and background colors")
    parser.add_argument("--background", help="force the page background, e.g. #FFFFFF for a white rebuild")
    parser.add_argument("--font-cjk", default="Microsoft YaHei")
    parser.add_argument("--font-latin", default="Arial")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    ocr = json.loads(Path(args.ocr_json).read_text(encoding="utf-8"))
    image = None
    if args.image:
        import numpy as np
        from PIL import Image

        image = np.array(Image.open(args.image).convert("RGB"))
        if image.shape[1] != int(ocr["width_px"]) or image.shape[0] != int(ocr["height_px"]):
            parser.error("--image size does not match the OCR source size")
    payload = build_page_ir(ocr, {"cjk": args.font_cjk, "latin": args.font_latin}, image, args.background)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(out), "text_objects": len(payload["pages"][0]["objects"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
