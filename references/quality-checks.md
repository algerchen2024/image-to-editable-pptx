# Quality checks

Use this checklist before delivery.

## Text

- Exact visible wording is reproduced or explicitly marked as uncertain.
- Short labels do not wrap unexpectedly.
- Font size and weight match the source hierarchy.
- Text color, alignment, and baseline are visually consistent.
- Text boxes stay inside the slide and do not clip glyphs.

## Geometry

- Source aspect ratio is preserved.
- Major margins and anchors match.
- Repeated cards have consistent size and spacing.
- Separator lines and arrows meet the intended objects.
- Borders, corner radii, and stroke weights are visually close.

## Images

- Complex visual assets contain no neighboring text or structure.
- No asset is stretched unless the source is stretched.
- No full-page screenshot is used to fake fidelity.

## Background

- Pure white means `#FFFFFF`, with no scan tint or screenshot shadow.
- Preserve intentional colored panels and fills.
- Remove watermarks only when the user requests a clean reconstruction and the watermark is not part of the intended slide content.

## Object fit (1:1)

`render_compare.py --page-ir` and `fidelity_loop.py` measure every object in the source and in the render:

- shapes, lines, images: all four ink edges within 2 px (or 0.2% of page width);
- text: first-line glyph height within 4%, same line count, line pitch within 3%, first-line width within 1.5%, first-line anchor within 2 px.

Text is also checked for stroke weight. When the source is clearly heavier or lighter, the loop toggles `bold`. When that is not enough, `font_hints` asks for a heavier or lighter family, such as a Heavy weight of Source Han Sans / Noto Sans CJK installed on the user's machine.

`objects_off` / `worst` list what does not match yet. Every entry must be fixed or explained in the delivery report. Typical explanations: a substituted font (see `fonts.substitutions`), a hairline whose anti-aliasing differs, an image asset edge.

## Delivery gate

Run `scripts/delivery_gate.py` with each slide's fidelity-loop workdir before delivering. It grades every slide:
- **HIGH**: may be called high fidelity.
- **CLOSE**: object fit below 0.9, or more than 3% of the source's visible content not covered by any PageIR object, i.e. something was left out; it lists the regions.
- **FAIL**: the fidelity loop never ran, PageIR or font errors, inspection failed, or render problems.

FAIL slides are not delivered. Put the gate table in the delivery report.

## Render comparison

The provided comparison script reports, per slide:

- normalized mean absolute error and edge F1;
- an aspect-ratio check (the script exits non-zero on a mismatch, or when the slide count differs from the number of source images);
- `hotspots`: the grid regions with the largest difference, with the PageIR object ids they overlap when `--page-ir` is given.

Treat the scores as review signals, not universal pass/fail thresholds. Fonts and renderer differences can change pixel metrics even when the slide is structurally correct.

Inspect the generated `rendered.png`, `overlay.png`, `heatmap.png`, `diff.png`, and `metrics.json` before declaring strict fidelity. Each remaining hotspot should have an explanation.

## Structural inspection

`inspect_pptx.py` fails when a shape leaves the canvas, or when pictures cover 95% or more of a slide. That covers a single full-slide picture and several tiles that together rebuild a screenshot (`picture_union_coverage`).
