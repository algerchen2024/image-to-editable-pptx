# Reconstruction workflow

## 1. Inspect the source

Record page dimensions, background, title block, major horizontal and vertical anchors, repeated modules, and the smallest readable text. Identify whether the source is a clean slide export, screenshot, scan, or photographed page.

## 2. Establish anchors before details

Build the major geometry first: page margins, title baseline, separators, large panels, columns, rows, and primary arrows. Repeated modules should share measured dimensions and spacing.

## 3. Reconstruct text line by line

Use OCR only as a candidate transcription. `scripts/ocr_to_pageir.py` turns `ocr_lines.py` output into draft text objects with estimated font sizes and sampled colors. Treat that draft as a starting point: every object's `note` shows its OCR confidence. Compare every visible string with the source. Preserve punctuation, capitalization, Chinese/English spacing, line breaks, and emphasized runs; use `runs` for mixed styling within a line. Prefer one object per source line for short labels, with `wrap: false`.

`ocr_lines.py` defaults to Tesseract `--psm 11` (sparse text), which suits multi-column slides, and upscales 2x before recognition. Use `--psm 6` for a single dense text block, and a higher `--scale` for very small captions.

## 4. Reconstruct simple geometry natively

Use PowerPoint shapes for rectangles, rounded cards, borders, arrows, connectors, and basic geometric symbols. This keeps the slide editable and avoids fuzzy raster edges. Measure fills, strokes, and text colors with `scripts/sample_colors.py` rather than estimating them by eye. It reports the border (background) color and the dominant distinct (foreground) color of a box. Measure the corner radius of rounded cards and set `corner_radius_px`.

## 5. Isolate complex visuals

Use a cropped/transparent image only when the visual cannot be represented reliably with simple geometry. `scripts/crop_asset.py` crops with the same source-pixel bbox used in PageIR, optionally turning the near-white surround transparent (`--white-to-alpha 245`), and prints the matching image object. Ensure the asset does not contain neighboring text, separators, or unrelated structure.

## 6. Compile from PageIR

Validate PageIR first, then compile. Do not silently repair wording or geometry inside the compiler. Put all page-specific decisions in PageIR.

## 7. Render and compare

Render the PPTX to an image and compare it to the source at the same pixel dimensions. For multi-page decks, pass one source image per slide in order. Pass `--page-ir` so each difference hotspot in `metrics.json` lists the object ids it overlaps. Review the full-page difference, `overlay.png`, `heatmap.png`, and local areas around text baselines, thin rules, icon edges, and module boundaries.

## 8. Repair the owning layer

- Wrong wording -> fix text transcription.
- Wrapping or baseline drift -> fix text bbox/font metrics.
- Shape displacement -> fix PageIR geometry.
- Wrong color or stroke -> fix PageIR style.
- Complex visual contamination -> rebuild the asset.
- White background request not satisfied -> set the page background and remove non-semantic screenshot texture.

## 9. Final check

Confirm editable text, native structures, no hidden full-slide screenshot, no overflow, and no unexplained large difference regions. Re-run the validators after the final edit.
