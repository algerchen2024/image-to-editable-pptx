---
name: image-to-editable-pptx
description: Rebuild flattened slide images, screenshots, scanned presentation pages, or image-based PPTX pages into source-faithful editable PowerPoint files. Use when an agent needs to recreate slide text as editable text, rebuild simple lines/panels/arrows as native PowerPoint shapes, preserve the original page ratio and layout, optionally force a pure white background, and validate the rendered result against the source image before delivery.
---

# Image to Editable PPTX

Reconstruct the visible content of a flattened slide into an editable `.pptx` without redesigning it. Preserve source wording, hierarchy, geometry, and visual rhythm; do not claim recovery of hidden vectors, original chart data, masters, animations, or inaccessible source content.

## Core rules

- Treat the supplied raster or image-based slide as the visual source of truth.
- Recreate readable text as editable text boxes.
- Recreate simple panels, borders, separators, arrows, and geometric icons as native PowerPoint shapes.
- Use raster assets only for complex visuals that cannot be reproduced reliably with native shapes.
- Never use the entire source image, or tiles of it, as the visible slide background when the user asks for an editable reconstruction.
- Never replace an icon with an emoji, font glyph, or unrelated symbol.
- Preserve the source aspect ratio and use source-pixel coordinates as the canonical geometry space.
- Use a pure white background when the user explicitly asks for white/clean background; otherwise preserve the visible source background.
- Keep uncertain OCR or visual interpretation explicit and review it against the source before delivery.

## Workflow

1. Run a runtime check when code execution is available:

```bash
python3 scripts/runtime_check.py --ocr-lang chi_sim+eng
```

2. Inspect the source image visually. Record the source width and height.
3. Extract candidate text and turn it into a draft PageIR (estimated font sizes, sampled text and page colors):

```bash
python3 scripts/ocr_lines.py source.png --lang chi_sim+eng --out analysis/ocr_lines.json
python3 scripts/ocr_to_pageir.py analysis/ocr_lines.json --image source.png --out page_ir.json
```

Treat the draft only as evidence. Every text object carries a `note` with its OCR confidence; correct wording, punctuation, spacing, line breaks, weight, and font against the source, then delete the notes you have resolved.

4. Complete the PageIR using `references/pageir-schema.md`: add panels, lines, and arrows; represent each visible object once. Measure colors instead of guessing, and crop complex visuals from the source so the asset matches its frame:

```bash
python3 scripts/sample_colors.py source.png --bbox 100 220 360 180
python3 scripts/crop_asset.py source.png --bbox 1200 160 210 210 --out assets/logo.png --white-to-alpha 245 --relative-to .
```

5. Validate the PageIR (warnings flag stretched image assets):

```bash
python3 scripts/validate_page_ir.py page_ir.json
```

6. Compile the PageIR into PowerPoint:

```bash
node scripts/compile_page_ir.js page_ir.json output.pptx
```

7. Inspect the compiled deck for canvas overflow and full-slide or tiled image shortcuts:

```bash
python3 scripts/inspect_pptx.py output.pptx
```

8. Render and compare the result when LibreOffice and Poppler are available. Pass one source image per slide, in order:

```bash
python3 scripts/render_compare.py source.png output.pptx --outdir quality --page-ir page_ir.json
```

9. Review `rendered.png`, `overlay.png`, and `heatmap.png`. `metrics.json` lists `hotspots`, the largest-difference regions with the PageIR object ids they overlap, so repair those objects first. Fix PageIR geometry or styling rather than hiding differences with a screenshot.
10. Re-run validation, compilation, and comparison after every material repair.
11. Deliver only the final editable `.pptx` and disclose any unresolved model-inferred text or visual approximation.

## Minimal PageIR

```json
{
  "schema_version": "1.1",
  "pages": [{
    "id": "slide-1", "width_px": 1600, "height_px": 900, "background": "#FFFFFF",
    "objects": [
      {"id": "card", "type": "round_rect", "bbox": [100, 220, 360, 180], "z": 1,
       "style": {"fill": "#FFF4EF", "line": "#F05A28", "line_width_pt": 1.5, "corner_radius_px": 16}},
      {"id": "arrow", "type": "line", "points": [700, 310, 500, 310], "z": 2,
       "style": {"color": "#F05A28", "width_pt": 2, "end_arrow": "triangle"}},
      {"id": "title", "type": "text", "bbox": [54, 36, 1150, 60], "z": 10,
       "runs": [{"text": "Plain "}, {"text": "emphasis", "bold": true, "color": "#F05A28"}],
       "style": {"font_face": "Microsoft YaHei", "font_size_pt": 30, "color": "#111111", "wrap": false}}
    ]
  }]
}
```

See `examples/minimal/page_ir.json` and `examples/features/page_ir.json` for complete files.

## Representation decisions

Use these default mappings:

- `text`: titles, labels, captions, body copy, numbers, badges, table text. Use `runs` for mixed bold/color/size within one text box.
- `rect`, `round_rect`, `ellipse`, `triangle`, `chevron`: simple native geometry (`corner_radius_px`, `line_dash`, `rotation_deg` available).
- `line`: separators, connectors, arrows, underlines, simple strokes. Points may run in any direction; the arrowhead follows `end_arrow`.
- `image`: complex logos, illustrations, textured graphics, photos, or dense pictorial icons.

For a complex image object, use an asset with transparent or white-safe margins and place it in the exact source-pixel rectangle. Avoid crop/stretch behavior unless the source itself is visibly stretched.

## Text fidelity

- Use one text object per visible source line for titles, short labels, captions, and badges, and set `wrap: false` on them so renderer font differences cannot introduce a line break.
- Preserve visible line breaks.
- Do not auto-rewrite wording.
- Prefer explicit font size and box geometry over shrink-to-fit.
- Match boldness, alignment, color, line spacing, and approximate font family.
- If the exact font is unavailable, choose the nearest metric-compatible font and re-check the render.

## White-background requests

When the user asks for a pure white background:

- Set `page.background` to `#FFFFFF` (or pass `--background "#FFFFFF"` to `ocr_to_pageir.py`).
- Rebuild all foreground boxes and separators independently.
- Do not retain scanned paper tint, screenshot shadows, or watermarks as background texture.
- Do not remove foreground content that is genuinely part of the design.

## Quality gates

Before delivery, require all of the following:

- Source and output aspect ratios match within 0.1%.
- All readable source text intended to remain text is editable.
- `inspect_pptx.py` passes: no shape outside the canvas, no full-slide or tiled raster shortcut.
- No unintended object overlap or clipping is visible.
- Background matches the user request.
- Major anchors, columns, rows, arrows, and separators align with the source.
- Rendered differences are reviewed at full-slide and local-object level, and every remaining hotspot is explained (font substitution, anti-aliasing, intentional white background) rather than ignored.

Use `references/quality-checks.md` for the detailed review checklist.

## When to stop repairing

Stop iterating when the remaining hotspots are all explained as above and a further repair would not visibly change the slide. As a guide, a clean slide export usually reaches `edge_f1` ≥ 0.6 and `normalized_mae` ≤ 0.05; scans and photos score lower, so judge them visually. After about five repair rounds without visible improvement, deliver and list what is still approximate.

## When tools are missing

- No Tesseract or language pack: transcribe text visually and author PageIR by hand; say that OCR was not used.
- No LibreOffice or Poppler: still run `validate_page_ir.py` and `inspect_pptx.py`, and state in the final answer that no render comparison was performed. Do not claim strict visual fidelity.
- No Node.js or PptxGenJS: report that the compiler cannot run; do not substitute a screenshot-based deck.

## Output contract

Use a concise final response and link the generated `.pptx`. If a machine or visual gate cannot be completed, state the exact limitation instead of claiming strict fidelity.

## References

- Read `references/pageir-schema.md` when authoring or repairing PageIR.
- Read `references/workflow.md` for the full reconstruction and repair runbook.
- Read `references/quality-checks.md` before final delivery.
