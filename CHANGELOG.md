# Changelog

All notable changes to this project will be documented here.

## Version numbering

The skill appears in ChatGPT as "GPT Image to Editable PPTX V<major>". Versions V1–V4 were iterated privately before the clean-room public release, which was tagged `v0.1.0`. From V5 on, the public version continues that numbering: the major version in `package.json` always matches the `V<n>` in the display name in `agents/openai.yaml`, and the release tag is `v<major>.<minor>.<patch>`.

The PageIR `schema_version` (currently 1.1) is the version of the internal JSON format and is numbered independently.

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
