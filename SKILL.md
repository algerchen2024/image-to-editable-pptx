---
name: image-to-editable-pptx
description: Rebuild one or several flattened slide images, screenshots, scanned presentation pages, or image-based PPTX pages into source-faithful editable PowerPoint files. Use when an agent needs to recreate slide text as editable text, rebuild simple lines/panels/arrows as native PowerPoint shapes, preserve the original page ratio and layout, optionally force a pure white background, and validate the rendered result against the source image before delivery.
---

# Image to Editable PPTX

Reconstruct the visible content of a flattened slide into an editable `.pptx` that matches the source 1:1: the same wording, geometry, colors, and type sizes. Do not redesign. Do not claim recovery of hidden vectors, original chart data, masters, animations, or inaccessible source content.

**High fidelity is the default and is never traded for speed.** Do not offer or fall back to a quick, low-precision conversion unless the user explicitly asks for a rough draft. A careful slide may take ten minutes or more, and that is expected: the user prefers one exact slide over several fast approximate ones.

Fidelity comes from measuring, not estimating: shapes are measured from the source pixels (`detect_shapes.py`), and every object is then measured again in the rendered result and corrected automatically (`fidelity_loop.py`). Your job is the part tools cannot do: correct wording, choose fonts, decide what is an image, and review what remains.

## Core rules

- Treat the supplied raster or image-based slide as the visual source of truth.
- Recreate readable text as editable text boxes.
- Recreate simple panels, borders, separators, arrows, and geometric icons as native PowerPoint shapes.
- Use raster assets only for complex visuals that cannot be reproduced reliably with native shapes.
- Never place the source image, or tiles of it, anywhere in the deck as a background or "fidelity layer". This includes temporary drafts, and images hidden behind other objects. `inspect_pptx.py` detects pictures that cover the slide at any z-order. Start directly with the PageIR workflow below; there is no screenshot-first phase.
- Never replace an icon with an emoji, font glyph, or unrelated symbol.
- Preserve the source aspect ratio and use source-pixel coordinates as the canonical geometry space.
- Use a pure white background when the user explicitly asks for white/clean background; otherwise preserve the visible source background.
- Keep uncertain OCR or visual interpretation explicit and review it against the source before delivery.

## Workflow

1. Settle the target machine and fonts first (see **Fonts for the user's machine**): the platform the user opens PowerPoint on (default `mac`), fonts they have installed, and any font they prefer per style. Then check the runtime and note `ready_render_compare`, `cjk_render_ready`, and `fonts`:

```bash
python3 scripts/runtime_check.py --ocr-lang chi_sim+eng --target mac --installed-fonts "Microsoft YaHei,STKaiti"
```

`fonts.exact_match_fonts` lists fonts that are on the user's machine **and** in this environment; text in those fonts is verified exactly as the user will see it. `fonts.recommended` gives the font to use per style and whether its render is exact.

If `cjk_render_ready` is false and the slide has Chinese text, install a CJK font for rendering (for example Noto Sans CJK, Source Han Sans, or WenQuanYi) **before** the first render; otherwise Chinese renders blank. A font installed only for rendering never goes into PageIR as `font_face`; see the next section.

2. Inspect the source image visually. Record the source width and height. With several pages, keep one source image per slide, in order.
3. Draft the text from OCR (estimated font sizes, sampled text and page colors):

```bash
python3 scripts/ocr_lines.py source.png --lang chi_sim+eng --out analysis/ocr_lines.json
python3 scripts/ocr_to_pageir.py analysis/ocr_lines.json --image source.png --target mac \
  --installed-fonts "Microsoft YaHei,STKaiti" --prefer "sans=Microsoft YaHei,kai=STKaiti" --cjk-style sans --out page_ir.json
```

Treat the draft as evidence. Every text object carries a `note` with its OCR confidence. Correct wording, punctuation, line breaks, and weight against the source. Merge lines that belong to one paragraph. Put each visual phrase in **one** text object with `runs`: a number and its unit (`1,000` + `项`), a label and its count (`剔除【不合适】` + `127项`), a brand and its title (`WorkBuddy` + `平安内网…`). Separate boxes collide or drift apart as soon as the viewer's font differs from the render font, and the validator warns about them. Delete the notes you have resolved.

4. Measure the shapes and add them to the draft. Panels, bordered cards, circles, and horizontal/vertical rules come back with measured position, size, fill, border color, stroke width, and corner radius:

```bash
python3 scripts/detect_shapes.py source.png --merge page_ir.json --out page_ir.json
```

Then add what detection cannot see: arrows (set `end_arrow`), diagonal connectors, dashed borders, gradients, and icons. Measure any remaining colors with `sample_colors.py` instead of guessing. Crop complex visuals from the source so each asset matches its frame:

```bash
python3 scripts/sample_colors.py source.png --bbox 100 220 360 180
python3 scripts/crop_asset.py source.png --bbox 1200 160 210 210 --out assets/logo.png --white-to-alpha 245 --relative-to .
```

5. Validate the PageIR (warnings flag stretched image assets):

```bash
python3 scripts/validate_page_ir.py page_ir.json
```

6. Run the fidelity loop. It compiles, renders, measures every object against the source, and corrects position, size, font size, bold, wrapping, line spacing, and letter spacing, keeping the best round. Do not hand-tune sizes or positions instead of running it; after any manual edit, run it again and deliver its `page_ir.best.json`:

```bash
python3 scripts/fidelity_loop.py page_ir.json source.png --out output.pptx --workdir quality
cp quality/page_ir.best.json page_ir.json
```

For several pages, pass all source images in slide order after the PageIR.

7. Review `quality/final/` for each slide: `overlay.png` (source and result blended), `heatmap.png`, the `objects_off` list, and `font_hints`. A font hint means the source strokes are heavier (or lighter) than the chosen font can produce even after toggling bold: choose a heavier family or weight (see the next section). The loop cannot fix wording, font family, color, missing or extra objects, or anything the source shows that PageIR lacks. Fix those in PageIR and run the loop again.
8. Inspect the final deck structurally:

```bash
python3 scripts/inspect_pptx.py output.pptx
```

9. Run `python3 scripts/delivery_gate.py quality` (one workdir per slide) and fix anything it reports. Then deliver the `.pptx` with the delivery report below, including the gate table.

Without the loop (for example to try one manual change), `node scripts/compile_page_ir.js` compiles. `render_compare.py source.png output.pptx --outdir quality --page-ir page_ir.json` measures. `refine_pageir.py quality/metrics.json page_ir.json --out page_ir.json` applies one round of corrections.

## Fonts for the user's machine

The PPTX is opened on the user's computer, not in the sandbox where it is rendered for checking. Every `font_face` must exist on that computer; otherwise PowerPoint substitutes it there and numbers, units, and titles collide.

**The user's font profile.** Before choosing fonts, look for what the user has said about their setup: in this conversation, in their custom instructions, or in memory. That means their platform, the fonts installed on their machine, and the font they want per style (for example "黑体用微软雅黑，楷体用华文楷体"). Use it as given. Record it in PageIR:

```json
"target": {"platform": "mac", "installed_fonts": ["Microsoft YaHei", "STKaiti"],
           "font_preferences": {"sans": "Microsoft YaHei", "kai": "STKaiti"}}
```

Nothing about one user's setup belongs in the skill itself. Without a profile, use `mac` and the defaults below.

**Match the typeface style.** Identify the style of each text group in the source: 黑体 (`sans`), 楷体 (`kai`), 宋体 (`song`), 仿宋 (`fangsong`), and for Latin text and numbers `latin_sans` or `latin_serif`. Pick the font for that style in this order:
1. the user's preference;
2. a font of that style on the user's machine that this environment also has (`exact_match_fonts`), so the render is exact;
3. the platform default below.

`fonts.recommended` from `runtime_check.py` applies this order for you.

| Style | mac | windows |
|---|---|---|
| sans 黑体 | PingFang SC, Hiragino Sans GB | Microsoft YaHei, DengXian, SimHei |
| kai 楷体 | Kaiti SC, STKaiti | KaiTi, STKaiti (with Office) |
| song 宋体 | Songti SC, STSong | SimSun, STSong (with Office) |
| fangsong 仿宋 | STFangsong | FangSong, STFangsong (with Office) |
| latin_sans | Helvetica Neue, Arial | Arial, Segoe UI |
| latin_serif | Times New Roman, Georgia | Times New Roman, Georgia |

Never substitute a different style to get an exact render: a 楷体 title stays 楷体 even if only 黑体 renders exactly.
- Never write a sandbox font (`Noto Sans CJK SC`, `WenQuanYi`, `DejaVu`, `Liberation`, …) into PageIR because it renders nicely here. The validator rejects it unless it is listed in `target.installed_fonts`, meaning the user has installed it too.
- When the chosen font is not in this environment, the sandbox renders a substitute, so the loop matches size and position but skips letter-spacing tuning. Expect small differences in line length on the user's machine, and say so in the report.

**Strict 1:1, and heavy display type.** PingFang SC stops at Semibold, but many designed slides use a heavier Chinese weight. For an exact match, use an open-licensed family that exists both here and on the user's Mac, for example **Noto Sans CJK SC / Source Han Sans SC (思源黑体)**. It is free to install on macOS and Windows and has Bold, Heavy, and Black weights.
1. Ask the user to install it on their Mac.
2. If the sandbox lacks it, install the same font files here with `python3 scripts/install_fonts.py <font files or folder>`.
3. Use that family name as `font_face` and list it in `target.installed_fonts`.

The loop then tunes against the exact font the user will see. Do not copy Apple or Microsoft system fonts into the sandbox: their licenses do not allow it.

## Several images (batch requests)

When the user sends several slide images, every slide gets the complete workflow above, one slide at a time. Batch size never changes the method or the bar.

1. Give each slide its own folder (`work/slide-01/`, `work/slide-02/`, …) with its source image, its own PageIR, and its own `fidelity_loop.py --workdir work/slide-NN/quality` run. Do not draft all slides first and refine later, and do not share one PageIR across different slides.
2. Finish a slide (loop run, overlay reviewed, gate passed) before starting the next. Tell the user briefly when each slide is done.
3. When all slides are done, check them together and join them into one deck:

```bash
python3 scripts/delivery_gate.py work/slide-01/quality work/slide-02/quality
python3 scripts/assemble_deck.py work/slide-01/quality/page_ir.best.json work/slide-02/quality/page_ir.best.json --out deck.pptx
```

Slides with a different aspect ratio go into a separate deck.

4. If time or context runs out, deliver the slides that pass the gate and list the remaining ones as not yet converted. Never finish the remaining slides at lower quality to complete the batch. Continue them in the next turn when the user agrees.

`delivery_gate.py` is required for single slides too. Per slide it reports:
- **HIGH**: the slide may be called high fidelity.
- **CLOSE**: object fit below 0.9, or more than 3% of the source's visible content not covered by any PageIR object (content was left out; it lists where).
- **FAIL**: the fidelity loop never ran, PageIR or font errors, inspection failed, or render problems.

FAIL slides are not delivered. CLOSE slides are delivered only as close reconstructions, with the reason.

## Minimal PageIR

```json
{
  "schema_version": "1.1",
  "target": {"platform": "mac", "installed_fonts": []},
  "pages": [{
    "id": "slide-1", "width_px": 1600, "height_px": 900, "background": "#FFFFFF",
    "objects": [
      {"id": "card", "type": "round_rect", "bbox": [100, 220, 360, 180], "z": 1,
       "style": {"fill": "#FFF4EF", "line": "#F05A28", "line_width_pt": 1.5, "corner_radius_px": 16}},
      {"id": "arrow", "type": "line", "points": [700, 310, 500, 310], "z": 2,
       "style": {"color": "#F05A28", "width_pt": 2, "end_arrow": "triangle"}},
      {"id": "title", "type": "text", "bbox": [54, 36, 1150, 60], "z": 10,
       "runs": [{"text": "WorkBuddy ", "font_face": "Helvetica Neue"}, {"text": "准入筛选", "color": "#F05A28"}],
       "style": {"font_face": "PingFang SC", "font_size_pt": 30, "bold": true, "color": "#111111", "wrap": false}},
      {"id": "total", "type": "text", "bbox": [54, 400, 300, 90], "z": 10,
       "runs": [{"text": "1,000", "font_face": "Helvetica Neue", "font_size_pt": 54}, {"text": "项", "font_size_pt": 20}],
       "style": {"font_face": "PingFang SC", "bold": true, "color": "#1E7B45", "valign": "bottom", "wrap": false}}
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

**Tables and UI screenshots.** A table with readable text is content the user will want to edit. Rebuild it as text objects plus rules and cell fills. `detect_shapes.py` measures the grid lines and cell fills; `ocr_to_pageir.py` drafts the cell text. Keep a table or an embedded UI screenshot as an image only when its text is too small or dense to rebuild reliably. If you do, list it in the delivery report as an image region with the reason, and offer to rebuild it.

## Text fidelity

- Use one text object per visible source line for titles, short labels, captions, and badges, and set `wrap: false` on them so renderer font differences cannot introduce a line break.
- Preserve visible line breaks.
- Do not auto-rewrite wording.
- Prefer explicit font size and box geometry over shrink-to-fit.
- Match boldness, alignment, color, and line spacing; take the family from **Fonts for the user's machine**.
- Never change `font_face` to suit the render environment. When the render environment lacks the font, `render_compare.py` reports it under `fonts.substitutions`. The loop then skips width and letter-spacing tuning for that font, because tuning against a substitute's metrics would make the real PowerPoint wrong. Metric-compatible substitutes (Arial ↔ Liberation Sans, Calibri ↔ Carlito, and so on) are still tuned.

## White-background requests

When the user asks for a pure white background:

- Set `page.background` to `#FFFFFF` (or pass `--background "#FFFFFF"` to `ocr_to_pageir.py`).
- Rebuild all foreground boxes and separators independently.
- Do not retain scanned paper tint, screenshot shadows, or watermarks as background texture.
- Do not remove foreground content that is genuinely part of the design.

## 1:1 tolerances and quality gates

An object matches when its rendered ink lies within **2 px or 0.2% of the page width**, whichever is larger, of the source ink on every edge. For text, the glyph height must also be within 4%, the first-line width within 1.5%, and the line count and line pitch must match. `object_fit_ratio` is the share of measured objects that match.

Before delivery, require all of the following:

- Source and output aspect ratios match within 0.1%.
- All readable source text intended to remain text is editable.
- `inspect_pptx.py` passes: no shape outside the canvas, no full-slide or tiled raster shortcut.
- `validate_page_ir.py` passes with no font errors (every `font_face` exists on the target machine) and no unresolved same-line warnings.
- `render_compare` / `fidelity_loop` report no `problems` (missing CJK font, slide-count or aspect mismatch).
- The delivered deck is the `fidelity_loop` output built from `page_ir.best.json`.
- Every object in `objects_off` is either fixed or explained in the delivery report.
- Background matches the user request.

## When to stop repairing

The loop stops by itself when every object matches or corrections stop helping. Stop your own repairs when `objects_off` is empty, or every remaining entry is explained (substituted font, anti-aliased hairline, asset edge) and a further change would not be visible in `overlay.png`. For clean digital slide exports, a good result typically reaches `object_fit_ratio` ≥ 0.9 and `edge_f1` ≥ 0.85. Scans and photos score lower; judge them from the overlay.

Only call the result "high fidelity" or "1:1" when `object_fit_ratio` ≥ 0.9. Below that, deliver it as a close reconstruction, list the largest remaining differences, and say what would close them: usually the font route above, or objects the loop cannot fix.

## When tools are missing

- No Tesseract or language pack: transcribe text visually and author PageIR by hand; say that OCR was not used.
- No CJK font for rendering: install one (see workflow step 1). If that is impossible, the comparison of Chinese text is invalid; say so.
- No LibreOffice or Poppler: `fidelity_loop.py` and `render_compare.py` cannot run. Still run `detect_shapes.py`, `validate_page_ir.py`, and `inspect_pptx.py`. Say in the delivery report that no render comparison was performed, and do not claim 1:1 fidelity.
- OpenCV is **not** required; all image tools use numpy and Pillow only.
- No Node.js or PptxGenJS: report that the compiler cannot run; do not substitute a screenshot-based deck.

## Delivery report

End with the `.pptx` link and this report, filled with real numbers. Write "not measured" where a step could not run; never round a missing measurement up to "passed".

```text
Gate: <delivery_gate table: level, object fit, uncovered ink, edge F1 per slide>
Pages: <n>, aspect <w:h> (matches source: yes/no)
Per slide: editable text objects <n>, native shapes <n>, image objects <n>
Fidelity loop: <rounds> rounds, best round <k>, delivered from page_ir.best.json: yes/no
Fidelity (final): slide 1 object fit <ok>/<measured>, edge_f1 <x.xx>, MAE <x.xxxx>; ...
Objects still off: <ids and why> | none
Font hints: <ids: heavier/lighter weight needed> | none
Image regions kept as pictures: <what, why> | none
Target: <mac|windows>; fonts in the PPTX: <names> (all available on target: yes/no)
Font profile used: <user preferences / installed fonts, or "defaults">
Verification render: <font -> rendered with, exact yes/no> per font
Not verified: <steps that could not run and why> | none
```

## References

- Read `references/pageir-schema.md` when authoring or repairing PageIR.
- Read `references/workflow.md` for the full reconstruction and repair runbook.
- Read `references/quality-checks.md` before final delivery.
