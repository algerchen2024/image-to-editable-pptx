# Changelog

All notable changes to this project will be documented here.

## Version numbering

The skill appears in ChatGPT as "GPT Image to Editable PPTX V<major>". Versions V1–V4 were iterated privately before the clean-room public release, which was tagged `v0.1.0`. From V5 on, the public version continues that numbering: the display name in `agents/openai.yaml` ends with `V<major>` (or `V<major>.<minor>` for a minor release, e.g. V5.1), matching `package.json`, and the release tag is `v<major>.<minor>.<patch>`.

The PageIR `schema_version` (currently 1.1) is the version of the internal JSON format and is numbered independently.

## [5.4.1] - 2026-10-12 (V5.4.1)

From two local field tests and two ChatGPT runs of V5.4 (both regression slides ended CLOSE at 0.58 and 0.82).

Fonts, decoupled from any one user's setup:
- New neutral target `any`: text uses theme fonts (`+mn-ea` / `+mn-lt`, titles `+mj-*`) and the theme carries no CJK font name, so the opening computer's system font is used. Use `mac` / `windows` only when the user says so.
- East Asian and Latin slots are written separately (`a:ea` = `font_face`, `a:latin` / `a:cs` = new `latin_font_face`); Chinese fonts no longer set Latin text and digits.
- Platform defaults list only fonts that ship with the OS (Office-only fonts removed). Docs use placeholders instead of example user fonts, and the skill never asks users to install fonts.
- Verification renders use a separate render copy: fonts LibreOffice cannot draw are swapped for the closest drawable font of the same style, and the delivered PPTX keeps the delivery fonts. `render_fonts.py` confirms drawable fonts with a LibreOffice + `pdffonts` probe (macOS LibreOffice cannot draw system fonts such as PingFang or Helvetica Neue; V5.4 silently rendered Latin text in a serif fallback).
- Width and weight are not tuned against substitutes (`width_unverified`, `weight_unverified`); the gate notes this instead of downgrading.
- `runtime_check.py`: `ready_ocr` (pytesseract + Tesseract + language packs), `fonts.render_drawable`, `fonts.target_detected`, and hints.

Fidelity loop:
- Text measurement masks neighbouring text, images, shape borders and line strokes, keeps only ink clusters joined to the object (gap ≤ 0.9 em), and rejects implausible glyph heights. Fixes the ~3× title enlargement and drifting axis labels.
- Font size changes are capped at ±15% per round and [0.5, 1.6]× overall; an object whose fit gets worse is restored, and frozen after a second strike.
- Spaces between CJK and Latin/digits are kept only when the source shows a gap.
- Arrows: the whole head is measured; open (`end_arrow: "open"`) and filled heads; `head_w` / `head_len` presets and custom `head_size_px` drawn as native shapes; stroke width is measured and corrected.
- `extract_assets.py` finds icons inside cards that are already in PageIR (only text and image regions are excluded).

Regression (two GPT-generated slides, `mac` / `any` / `windows`): all HIGH, object fit 1.0 / 1.0 / 0.95 and 0.95 / 0.95 / 0.95.

## [5.4.0] - 2026-10-11 (V5.4)

From a ChatGPT run where the skill's scripts never ran: the model hand-wrote python-pptx code, put the source image under a text layer, replaced icons with "◯", and reported a gate result it had not computed. The likely root cause was the Node compiler: PptxGenJS is not in the skill package and cannot be installed without internet. Shape detection was also noisy on AI-generated slides.

### Added

- `compile_page_ir.py`: pure-Python compiler (python-pptx), render-identical to the Node compiler; it is now the default for `fidelity_loop.py` and `assemble_deck.py`. Node.js is optional and the core runtime needs only numpy, Pillow, python-pptx and lxml.
- `extract_assets.py`: crops icons and other complex graphics whole, with transparent background, from ink no object covers. Solid bars and hairlines are left for native shapes; overlapping parts merge; card borders and axes are masked so they never bridge clusters.
- Shape-based hairline detector in `detect_shapes.py`: 1-4 px lines found by form, not color, with their true color. It catches light-gray card borders and chart axes on anti-aliased images. Four border lines reassemble into one rounded outline with a measured radius, and axis segments interrupted by bars are joined.
- SKILL.md "Running this skill": how to locate the scripts; never improvise (no hand-written python-pptx, no screenshot layer, no glyph icons); stop and report when a step fails; gate results only quoted from `delivery_gate.py`. Text comes first, then icons, then shapes.

### Fixed

- Detection: candidate colors counted outside text and asset regions; contrast-relative color tolerances and deduplication; anti-aliasing halos dropped only when a stronger color runs beside them; anti-aliased corners no longer become rounded rectangles.
- Fit: shapes are measured by their own color (largest component), so overlapping or overflowing text no longer moves cards. Text in fonts the render environment substitutes is marked `width_unverified` instead of failing line-width checks; the gate reports how many.
- Python compiler drops the theme style that added drop shadows to shapes.

## [5.3.0] - 2026-10-10 (V5.3)

Font profiles and batch quality. Users with their own fonts get exact renders without anything user-specific in the skill. Several images no longer lead to fast, low-fidelity conversions.

### Added

- Typeface styles (`sans` 黑体, `kai` 楷体, `song` 宋体, `fangsong` 仿宋, `latin_sans`, `latin_serif`) with per-platform candidates. `choose_font` prefers, in order: the user's stated font, then a font of that style that is both on the user's machine and in the render environment (exact render), then the platform default. It never switches style to get an exact render.
- `target.font_preferences` in PageIR; `runtime_check.py --target --installed-fonts` reports `fonts.exact_match_fonts` and `fonts.recommended` per style; `ocr_to_pageir.py --installed-fonts --prefer --cjk-style`.
- SKILL.md: read the user's font profile from the conversation, custom instructions, or memory; style table; match the source's typeface style.
- `delivery_gate.py`: grades each slide HIGH / CLOSE / FAIL. Slides that skipped the fidelity loop, have font or PageIR errors, or fail inspection FAIL. Slides with object fit < 0.9 or > 3% uncovered source content are CLOSE, with the missing regions listed.
- `assemble_deck.py`: join per-slide results into one deck (same aspect ratio; fonts and preferences merged).
- SKILL.md: high fidelity is the default and never traded for speed. Batch requests run the full workflow one slide at a time, each in its own folder. Slides that cannot be finished are listed, never done at lower quality.
- `fidelity_loop.py` writes `fidelity_summary.json` and absolute asset paths in `page_ir.best.json`.
- Text measurement widens its window when glyphs are clipped at its side (overflowing single-line titles).

## [5.2.0] - 2026-10-10 (V5.2)

From a real ChatGPT run (WorkBuddy slide): the deck used the sandbox font Noto Sans CJK SC, which a Mac or Windows PowerPoint does not have. Numbers and units sat in separate boxes and collided once the font changed. Headings were lighter than the source, and the loop's text measurement was thrown off by neighbouring lines and colored headers.

### Added

- Target machine in PageIR (`target.platform`, `target.installed_fonts`); default `mac`. The validator rejects render-sandbox fonts unless the user installed them, and warns about fonts that are not standard on the target.
- `ocr_to_pageir.py --target mac|windows` (default mac): PingFang SC / Helvetica Neue on mac, Microsoft YaHei / Arial on windows.
- `install_fonts.py`: install user-supplied, licensed font files (e.g. Source Han Sans / Noto Sans CJK, which the user also installs on their Mac) so the loop tunes against the exact delivery font.
- Same-line lint: separate text objects that read as one phrase (number + unit, label + count) raise a warning to merge them into one object with runs.
- Stroke-weight fitting: text ink density is compared with the source; the loop toggles bold, and `font_hints` asks for a heavier or lighter family when bold is not enough.
- SKILL.md: "Fonts for the user's machine" section with a strict 1:1 route, the runs rule, a mandatory fidelity-loop delivery, and "high fidelity" only at object fit ≥ 0.9.

### Fixed

- Text measurement: the background behind text is taken from inside the text box (white text on colored headers); lines are built from full-height glyphs only, so radicals or punctuation of a neighbouring line can no longer stretch a line; tighter vertical and wider horizontal search windows (overflowing single-line titles).
- Fidelity loop scoring includes mean per-object error, so partial progress is kept instead of being discarded.

## [5.1.0] - 2026-10-09 (V5.1)

Aimed at 1:1 fidelity and at the failures seen in a real ChatGPT run (missing OpenCV, blank Chinese renders, a screenshot "fidelity layer", tables kept as images, unverifiable delivery reports).

### Added

- `fidelity_loop.py`: compile -> render -> measure -> refine until every object matches the source within 2 px (or 0.2% of page width); keeps the best round.
- Per-object fit measurement (`object_fit.py`) in `render_compare.py --page-ir`: shapes, lines and images by their four ink edges; text line by line (glyph height -> font size, line count -> wrapping, line pitch -> line spacing, first-line width -> fine font size / letter spacing, first-line anchor -> position). Search windows widen when an object drifted or the ink is clipped.
- `refine_pageir.py`: apply one round of corrections from `metrics.json`.
- `detect_shapes.py`: measure flat panels, bordered cards (fill, border color, stroke width, corner radius), circles and horizontal/vertical rules from the source; ignores glyph strokes inside text boxes, removes anti-aliasing halos, keeps table grid lines on zebra rows.
- `font_check.py`: reports which font the render environment really uses per PageIR font, flags Chinese text without any CJK font, and knows metric-compatible substitutes (Arial/Liberation Sans, Calibri/Carlito, ...). Width and letter-spacing tuning is skipped for non-compatible substitutes so the PPTX stays right in PowerPoint.
- `runtime_check.py` reports `cjk_render_ready` and installed CJK fonts.
- SKILL.md: no screenshot "fidelity layer" at any stage, fonts rule, tables/UI screenshots rebuilt or disclosed, 1:1 tolerances, and a mandatory delivery report with real numbers.

### Changed

- OpenCV removed: all image processing uses numpy + Pillow (`image_ops.py`), so render comparison works in sandboxes without `cv2`. Edge F1 now uses a Sobel edge map with 1 px tolerance; scores are not directly comparable with 5.0.
- `render_compare.py` prints a compact summary; the full report stays in `metrics.json`.
- Display name `GPT Image to Editable PPTX V5.1`; `validate_skill.py` accepts `V<major>.<minor>` and checks it against `package.json`.

## [5.0.0] - 2026-10-09 (V5)

Supersedes V4 in ChatGPT. This release was briefly labelled `0.2.0` on its pull request; that label was never tagged or released.

### Fixed

- Right-to-left and bottom-to-top lines and arrows compiled with negative extents; they now use `flipH`/`flipV` and keep their direction.
- Line and shape dash styles were silently ignored (wrong PptxGenJS option name); `dot` now maps to a valid preset.
- Short hex colors (`#FFF`) passed validation but produced invalid colors in the compiled deck.
- `margin_pt` was converted to inches although PptxGenJS expects points, so text margins were ~72x too small.
- Style colors, font sizes, `z`, and zero-length lines were not validated.

### Added

- PageIR 1.1 (backward compatible): text `runs` for mixed styling, `line_spacing_multiple`, `char_spacing_pt`, `underline`, `wrap`, `rotation_deg`, `corner_radius_px`, `line_dash`, top-level `lang`/`title`/`default_font_face`, and per-page `lang`.
- Portrait pages compile to a 7.5 in tall slide instead of an oversized 13.33 in wide one.
- `ocr_to_pageir.py`: draft PageIR from OCR lines with estimated font sizes and sampled colors.
- `sample_colors.py`: background/foreground/dominant colors for source boxes.
- `crop_asset.py`: crop complex visuals by PageIR bbox with optional white-to-transparent.
- `render_compare.py`: multi-slide comparison, aspect-ratio check, overlay and heatmap images, difference hotspots mapped to PageIR object ids, source-resolution rendering.
- `inspect_pptx.py`: detects tiled screenshots through the union of picture coverage.
- `validate_page_ir.py`: warnings for image assets whose aspect ratio differs from their bbox.
- `ocr_lines.py`: configurable `--psm` (default 11, sparse text) and `--scale` upscaling; lower default confidence cutoff for CJK.
- Compiler round-trip tests, helper tests, a PageIR 1.1 feature fixture, a committed `package-lock.json`, and an end-to-end render check in CI.
- SKILL.md: platform-neutral description, minimal PageIR example, stopping rule, and fallback behavior when tools are missing.

## [0.1.0] - 2026-08-08

### Added

- Initial public clean-room release.
- ChatGPT Skill entrypoint and UI metadata.
- Source-pixel PageIR schema.
- PageIR validator.
- Generic PptxGenJS compiler for editable text, native shapes, lines/arrows, and image assets.
- Tesseract line OCR helper.
- Runtime preflight and render-comparison helper.
- Minimal fixture, unit tests, CI workflow, and contributor/security documentation.
- Bilingual README showcase assets with downloadable English and Chinese editable PPTX examples produced with the skill.
