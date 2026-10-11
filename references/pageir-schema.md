# PageIR schema

PageIR is the only page-specific construction input. Keep geometry in source pixels so the source image remains the canonical coordinate system.

Schema `1.1` is a backward-compatible superset of `1.0`: every 1.0 file is a valid 1.1 file. The validator accepts both versions.

## Top level

```json
{
  "schema_version": "1.1",
  "lang": "zh-CN",
  "title": "Optional deck title",
  "default_font_face": "Arial",
  "pages": []
}
```

`lang`, `title`, and `default_font_face` are optional (defaults: `zh-CN`, a generic title, `Arial`).

### Target machine and fonts

```json
"target": {"platform": "mac", "installed_fonts": ["Noto Sans CJK SC"]}
```

Optionally add `"font_preferences": {"sans": "Microsoft YaHei", "kai": "STKaiti"}`: the user's font per typeface style. Styles: `sans`, `kai`, `song`, `fangsong`, `latin_sans`, `latin_serif`.

`target` describes where the PPTX will be opened. `platform` is `mac`, `windows`, or `any`. `installed_fonts` lists extra fonts the user has installed there.

The validator then checks every `font_face`, including run fonts and `default_font_face`:
- Render-sandbox fonts (Noto, WenQuanYi, DejaVu, Liberation, …) are **errors** unless listed in `installed_fonts`.
- Fonts that are not standard on the target platform are **warnings**.

Defaults are `PingFang SC` / `Helvetica Neue` on mac, and `Microsoft YaHei` / `Arial` on windows.

Each page must contain:

```json
{
  "id": "slide-1",
  "width_px": 1600,
  "height_px": 900,
  "background": "#FFFFFF",
  "objects": []
}
```

All pages in one deck must have the same aspect ratio within 0.1%. A page may set its own `lang`.

Landscape pages compile to a 13.333 in wide slide; portrait pages compile to a 7.5 in tall slide. The other side follows the source aspect ratio.

## Colors

Colors are hex strings: `#RRGGBB` or the short form `#RGB`. `#FFF` is the same as `#FFFFFF`.

## Common object fields

Every object needs a unique `id` and a `type`. Objects with a rectangular extent use:

```json
"bbox": [x, y, width, height]
```

Coordinates are source pixels, with origin at the source image's top-left corner.

Optional fields on any object:

- `z`: back-to-front order. Lower values render first; equal values keep file order.
- `rotation_deg`: clockwise rotation around the bbox centre (not for `line`).
- `note`: free text for review, ignored by the compiler. `ocr_to_pageir.py` uses it to record OCR confidence.

## Text

```json
{
  "id": "title",
  "type": "text",
  "bbox": [54, 36, 1150, 92],
  "text": "Editable title",
  "style": {
    "font_face": "Microsoft YaHei",
    "font_size_pt": 30,
    "bold": true,
    "italic": false,
    "underline": false,
    "color": "#111111",
    "align": "left",
    "valign": "top",
    "margin_pt": 0,
    "line_spacing_multiple": 1.0,
    "char_spacing_pt": 0,
    "wrap": true
  }
}
```

- Supported alignments are `left`, `center`, `right`. Supported vertical alignments are `top`, `mid`, `bottom`.
- `margin_pt` is the inner padding on all sides, in points.
- `line_spacing_multiple` sets line spacing as a multiple of single spacing.
- `char_spacing_pt` adds letter spacing, in points.
- `wrap: false` keeps a single-line label on one line whatever the renderer's font metrics. Use it for titles, labels, and badges.
- Text never shrinks to fit; size the font explicitly.
- `\n` inside `text` starts a new paragraph.

### Mixed styles in one text box (`runs`)

Use `runs` instead of `text` when one line mixes weight, color, size, or font:

```json
{
  "id": "headline",
  "type": "text",
  "bbox": [54, 36, 1150, 60],
  "runs": [
    { "text": "Revenue up " },
    { "text": "32%", "bold": true, "color": "#F05A28" },
    { "text": " year over year" }
  ],
  "style": { "font_face": "Arial", "font_size_pt": 28, "color": "#111111", "wrap": false }
}
```

Each run may set `font_face`, `font_size_pt`, `bold`, `italic`, `underline`, `color`, and `char_spacing_pt`. Anything a run leaves out comes from the object's `style`.

Keep each visual phrase in one object with runs: a number and its unit, a label and its count, a brand and its title. The validator warns when separate text objects sit on one line, because they collide or drift apart when the viewer's font differs from the render font.

## Native shape

Use `rect`, `round_rect`, `ellipse`, `triangle`, or `chevron`.

```json
{
  "id": "panel",
  "type": "round_rect",
  "bbox": [100, 220, 360, 180],
  "style": {
    "fill": "#FFF4EF",
    "fill_transparency": 0,
    "line": "#F05A28",
    "line_width_pt": 1.5,
    "line_dash": "solid",
    "corner_radius_px": 16
  }
}
```

- Set `fill` to `null` for no fill and `line` to `null` for no outline.
- `fill_transparency` ranges from 0 (opaque) to 100 (invisible).
- `line_dash` accepts the same values as a line's `dash`.
- `corner_radius_px` is only valid on `round_rect`. It is the measured source corner radius, in pixels. Without it, PowerPoint's default rounding applies.

## Line or arrow

```json
{
  "id": "arrow-1",
  "type": "line",
  "points": [510, 300, 690, 300],
  "style": {
    "color": "#F05A28",
    "width_pt": 2.0,
    "dash": "solid",
    "start_arrow": "none",
    "end_arrow": "triangle"
  }
}
```

- `points` are `[x1, y1, x2, y2]` and may run in any direction: right-to-left and bottom-to-top lines keep their direction and arrowheads.
- Zero-length lines are rejected.
- Supported dash values are `solid`, `dash`, `dot`, and `dash_dot`.

## Image asset

Use image assets only for visual content that is not reliable as PowerPoint primitives.

```json
{
  "id": "illustration-1",
  "type": "image",
  "bbox": [1200, 160, 210, 210],
  "path": "assets/illustration-1.png"
}
```

`path` is relative to the PageIR file. The compiler places the image into the exact PageIR frame. The validator warns if the asset's aspect ratio differs from the bbox by more than 2%, because the asset would be stretched. `scripts/crop_asset.py` crops the asset from the source with the same bbox, so this cannot happen.

## Layering

Sort objects by `z` when overlap matters. Use this common order:

1. large background panels;
2. separators and connectors;
3. images and icons;
4. text.

Avoid duplicate objects that visually own the same source content.
