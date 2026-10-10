#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const PptxGenJS = require('pptxgenjs');

// Keep in sync with scripts/pageir_common.py.
const LANDSCAPE_WIDTH_IN = 13.333333;
const PORTRAIT_HEIGHT_IN = 7.5;

function fail(message) {
  console.error(message);
  process.exit(2);
}

function hex(value, fallback) {
  if (value === null || value === undefined) return fallback;
  let digits = String(value).trim().replace(/^#/, '');
  if (/^[0-9a-fA-F]{3}$/.test(digits)) digits = digits.split('').map((ch) => ch + ch).join('');
  if (!/^[0-9a-fA-F]{6}$/.test(digits)) fail(`Invalid hex color: ${value}`);
  return digits.toUpperCase();
}

function slideSize(widthPx, heightPx) {
  const aspect = widthPx / heightPx;
  if (aspect >= 1) return { w: LANDSCAPE_WIDTH_IN, h: LANDSCAPE_WIDTH_IN / aspect };
  return { w: PORTRAIT_HEIGHT_IN * aspect, h: PORTRAIT_HEIGHT_IN };
}

function pxBoxToInches(bbox, sx, sy) {
  return { x: bbox[0] * sx, y: bbox[1] * sy, w: bbox[2] * sx, h: bbox[3] * sy };
}

function mapShapeType(pptx, type) {
  const mapping = {
    rect: 'rect',
    round_rect: 'roundRect',
    ellipse: 'ellipse',
    triangle: 'triangle',
    chevron: 'chevron',
  };
  const key = mapping[type];
  if (!key || !pptx.ShapeType[key]) fail(`Unsupported shape type: ${type}`);
  return pptx.ShapeType[key];
}

function mapDash(value) {
  const mapping = { solid: 'solid', dash: 'dash', dot: 'sysDot', dash_dot: 'dashDot' };
  return mapping[value || 'solid'] || 'solid';
}

function mapArrow(value) {
  return value === 'triangle' ? 'triangle' : 'none';
}

function withRotation(opts, obj) {
  const rotation = Number(obj.rotation_deg || 0);
  if (rotation) opts.rotate = rotation;
  return opts;
}

function runOptions(style) {
  const opts = {};
  if (style.font_face !== undefined) opts.fontFace = style.font_face;
  if (style.font_size_pt !== undefined) opts.fontSize = Number(style.font_size_pt);
  if (style.bold !== undefined) opts.bold = Boolean(style.bold);
  if (style.italic !== undefined) opts.italic = Boolean(style.italic);
  if (style.underline) opts.underline = { style: 'sng' };
  if (style.color !== undefined) opts.color = hex(style.color, '111111');
  if (style.char_spacing_pt !== undefined) opts.charSpacing = Number(style.char_spacing_pt);
  return opts;
}

function textContent(obj) {
  if (!Array.isArray(obj.runs)) return obj.text || '';
  return obj.runs.map((run) => ({ text: String(run.text || ''), options: runOptions(run) }));
}

function addText(slide, obj, style, box, lang) {
  const opts = {
    ...box,
    fontFace: style.font_face || 'Arial',
    fontSize: Number(style.font_size_pt || 18),
    bold: Boolean(style.bold),
    italic: Boolean(style.italic),
    color: hex(style.color, '111111'),
    align: style.align || 'left',
    valign: style.valign || 'top',
    // PptxGenJS text margins are expressed in points.
    margin: Number(style.margin_pt || 0),
    paraSpaceBefore: 0,
    paraSpaceAfter: 0,
    lineSpacingMultiple: Number(style.line_spacing_multiple || 1.0),
    wrap: style.wrap !== false,
    fit: 'none',
    lang: style.lang || lang,
    isTextBox: true,
  };
  if (style.underline) opts.underline = { style: 'sng' };
  if (style.char_spacing_pt !== undefined) opts.charSpacing = Number(style.char_spacing_pt);
  slide.addText(textContent(obj), withRotation(opts, obj));
}

function addLine(slide, pptx, obj, style, sx, sy) {
  const [x1, y1, x2, y2] = obj.points.map(Number);
  // DrawingML cannot express negative extents: normalise the box and flip
  // it so the start/end (and arrowheads) keep their direction.
  const opts = {
    x: Math.min(x1, x2) * sx,
    y: Math.min(y1, y2) * sy,
    w: Math.abs(x2 - x1) * sx,
    h: Math.abs(y2 - y1) * sy,
    line: {
      color: hex(style.color, '111111'),
      width: Number(style.width_pt || 1),
      dashType: mapDash(style.dash),
      beginArrowType: mapArrow(style.start_arrow),
      endArrowType: mapArrow(style.end_arrow),
    },
  };
  if (x2 < x1) opts.flipH = true;
  if (y2 < y1) opts.flipV = true;
  slide.addShape(pptx.ShapeType.line, opts);
}

function addImage(slide, obj, box, baseDir) {
  const imagePath = path.isAbsolute(obj.path) ? obj.path : path.resolve(baseDir, obj.path);
  slide.addImage(withRotation({ path: imagePath, ...box }, obj));
}

function addShape(slide, pptx, obj, style, box, sx, sy) {
  const opts = { ...box };
  if (style.fill === null) opts.fill = { color: 'FFFFFF', transparency: 100 };
  else opts.fill = { color: hex(style.fill, 'FFFFFF'), transparency: Number(style.fill_transparency || 0) };
  if (style.line === null) opts.line = { color: 'FFFFFF', transparency: 100, width: 0 };
  else {
    opts.line = {
      color: hex(style.line, '111111'),
      width: Number(style.line_width_pt || 1),
      dashType: mapDash(style.line_dash),
    };
  }
  if (obj.type === 'round_rect' && style.corner_radius_px !== undefined) {
    // PptxGenJS expects the corner radius in inches.
    opts.rectRadius = Number(style.corner_radius_px) * Math.min(sx, sy);
  }
  slide.addShape(mapShapeType(pptx, obj.type), withRotation(opts, obj));
}

function addObject(slide, pptx, obj, sx, sy, baseDir, lang) {
  const style = obj.style || {};
  if (obj.type === 'line') return addLine(slide, pptx, obj, style, sx, sy);
  const box = pxBoxToInches(obj.bbox, sx, sy);
  if (obj.type === 'text') return addText(slide, obj, style, box, lang);
  if (obj.type === 'image') return addImage(slide, obj, box, baseDir);
  return addShape(slide, pptx, obj, style, box, sx, sy);
}

async function main() {
  const [, , irPathArg, outputPathArg] = process.argv;
  if (!irPathArg || !outputPathArg) fail('Usage: node scripts/compile_page_ir.js page_ir.json output.pptx');
  const irPath = path.resolve(irPathArg);
  const outputPath = path.resolve(outputPathArg);
  const payload = JSON.parse(fs.readFileSync(irPath, 'utf8'));
  if (!payload.pages || payload.pages.length === 0) fail('PageIR contains no pages');

  const first = payload.pages[0];
  const aspect = Number(first.width_px) / Number(first.height_px);
  if (!Number.isFinite(aspect) || aspect <= 0) fail('Invalid page dimensions');
  const { w: slideWidth, h: slideHeight } = slideSize(Number(first.width_px), Number(first.height_px));
  const lang = payload.lang || 'zh-CN';
  const defaultFont = payload.default_font_face || 'Arial';

  const pptx = new PptxGenJS();
  pptx.defineLayout({ name: 'SOURCE_RATIO', width: slideWidth, height: slideHeight });
  pptx.layout = 'SOURCE_RATIO';
  pptx.author = 'image-to-editable-pptx';
  pptx.subject = 'Editable reconstruction from PageIR';
  pptx.title = payload.title || 'Editable PowerPoint reconstruction';
  pptx.company = '';
  pptx.lang = lang;
  pptx.theme = { headFontFace: defaultFont, bodyFontFace: defaultFont, lang };

  const baseDir = path.dirname(irPath);
  for (const page of payload.pages) {
    const currentAspect = Number(page.width_px) / Number(page.height_px);
    if (Math.abs(currentAspect - aspect) / aspect > 0.001) fail('All pages must share the same aspect ratio');
    const slide = pptx.addSlide();
    slide.background = { color: hex(page.background || '#FFFFFF', 'FFFFFF') };
    const sx = slideWidth / Number(page.width_px);
    const sy = slideHeight / Number(page.height_px);
    // Stable sort: equal z keeps PageIR order.
    const objects = [...(page.objects || [])].sort((a, b) => Number(a.z || 0) - Number(b.z || 0));
    for (const obj of objects) addObject(slide, pptx, obj, sx, sy, baseDir, page.lang || lang);
  }

  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  await pptx.writeFile({ fileName: outputPath });
  console.log(JSON.stringify({ output: outputPath, slides: payload.pages.length, width_in: slideWidth, height_in: slideHeight }));
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
