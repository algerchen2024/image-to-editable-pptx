#!/usr/bin/env python3
"""Discover fonts LibreOffice can draw for verification renders.

The delivery font is never changed here.  This module only supplies a
render-time family inventory and style-aware substitutes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import ImageFont

from pageir_common import DEFAULT_FONTS, infer_style, is_latin_font, is_theme_font

_CACHE: dict[str, dict] = {}
CACHE_VERSION = 2
FONT_SUFFIXES = {".ttf", ".otf", ".ttc", ".otc"}
# Installed faces. On macOS without fontconfig these are probe candidates only.
SYSTEM_FONT_DIRS = (
    Path.home() / "Library" / "Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts"),
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
)
USER_FONT_DIRS = (
    Path.home() / "Library" / "Application Support" / "LibreOffice" / "4" / "user" / "fonts",
    Path.home() / ".config" / "libreoffice" / "4" / "user" / "fonts",
)
LINUX_BUNDLED_DIRS = (
    Path("/usr/lib/libreoffice/share/fonts/truetype"),
    Path("/usr/lib64/libreoffice/share/fonts/truetype"),
    Path("/usr/share/libreoffice/share/fonts/truetype"),
)
FONT_DIRS = SYSTEM_FONT_DIRS[:3] + USER_FONT_DIRS[:1]
SUBSET_PREFIX = re.compile(r"^[A-Za-z]{6}\+")

# Render substitutes, best first. Only a family LibreOffice can draw is used.
STYLE_CANDIDATES = {
    "sans": [
        "Noto Sans CJK SC", "Source Han Sans SC", "Source Han Sans",
        "WenQuanYi Zen Hei", "WenQuanYi Micro Hei",
        "PingFang SC", "Hiragino Sans GB", "Heiti SC", "Microsoft YaHei", "SimHei", "DengXian",
        "Arial", "Liberation Sans", "DejaVu Sans",
    ],
    "kai": [
        "KaiTi", "Kaiti SC", "STKaiti", "Noto Serif CJK SC", "Source Han Serif SC",
        "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei",
    ],
    "song": [
        "SimSun", "Songti SC", "STSong", "Noto Serif CJK SC", "Source Han Serif SC",
        "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei",
    ],
    "fangsong": [
        "FangSong", "STFangsong", "Noto Serif CJK SC", "Source Han Serif SC",
        "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei",
    ],
    "latin_sans": ["Liberation Sans", "Arial", "Helvetica Neue", "Helvetica", "Arimo", "DejaVu Sans"],
    "latin_serif": ["Liberation Serif", "Times New Roman", "Georgia", "Tinos", "DejaVu Serif"],
}


def normalize_family(name: str) -> str:
    """Lower case, with spaces and hyphens removed, for probe comparison."""
    return re.sub(r"[\s_\-]+", "", name).lower()


def pdf_font_key(token: str) -> str:
    """Family key of one pdffonts name: drop the XXXXXX+ subset tag and the style suffix."""
    name = SUBSET_PREFIX.sub("", token.strip())
    name = name.split("-", 1)[0]
    return normalize_family(name)


def match_probed_families(families: set[str], pdffonts_text: str) -> set[str]:
    """Families whose normalized name equals, or is a prefix of, an embedded font."""
    keys = []
    for line in pdffonts_text.splitlines():
        parts = line.split()
        if not parts:
            continue
        token = parts[0]
        if token.lower() == "name" or set(token) <= {"-"}:
            continue
        key = pdf_font_key(token)
        if key:
            keys.append(key)
    matched = set()
    for family in families:
        norm = normalize_family(family)
        if not norm:
            continue
        for key in keys:
            if norm == key or norm.startswith(key) or key.startswith(norm):
                matched.add(family)
                break
    return matched


def style_candidate_names() -> set[str]:
    names: set[str] = set()
    for values in STYLE_CANDIDATES.values():
        names.update(values)
    return names


def _fc_list() -> set[str] | None:
    exe = shutil.which("fc-list")
    if not exe:
        return None
    proc = subprocess.run([exe, ":", "family"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if proc.returncode:
        return None
    names = set()
    for line in proc.stdout.splitlines():
        names.update(x.strip() for x in line.split(",") if x.strip())
    return names


def _pillow_families(path: Path) -> set[str]:
    names: set[str] = set()
    for index in range(16):
        try:
            face = ImageFont.truetype(str(path), size=12, index=index)
        except Exception:
            if index:
                break
            continue
        name = face.getname()
        # getname() is (family, style). The style ("Bold", "Regular") is not a family.
        if name and name[0]:
            names.add(name[0])
    return names


def scan_font_dirs(dirs: tuple[Path, ...] | list[Path] = FONT_DIRS) -> set[str]:
    names: set[str] = set()
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.suffix.lower() in FONT_SUFFIXES:
                names.update(_pillow_families(path))
    return names


def libreoffice_font_dirs(soffice: str | None) -> list[Path]:
    """Bundled TrueType directory derived from the real soffice binary."""
    found: list[Path] = []
    app = None
    if soffice:
        real = Path(os.path.realpath(soffice))
        for parent in (real, *real.parents):
            if parent.name == "LibreOffice.app":
                app = parent
                break
    if app is None:
        mac_app = Path("/Applications/LibreOffice.app")
        if mac_app.exists():
            app = mac_app
    if app is not None:
        bundled = app / "Contents" / "Resources" / "fonts" / "truetype"
        if bundled.exists():
            found.append(bundled)
    for candidate in LINUX_BUNDLED_DIRS:
        if candidate.exists() and candidate not in found:
            found.append(candidate)
    return found


def user_font_dirs() -> list[Path]:
    return [path for path in USER_FONT_DIRS if path.exists()]


def select_drawable(
    *,
    platform: str,
    fontconfig: set[str] | None,
    probed: set[str] | None,
    bundled: set[str],
    user: set[str],
    system: set[str],
) -> set[str]:
    """Which families count as drawable.

    Linux (and any host) with fontconfig trusts fc-list. macOS without it
    trusts only families the probe embedded. If that probe fails, bundled and
    user-font directories still count; system directories never do.
    """
    if fontconfig is not None:
        return set(fontconfig)
    if probed is not None:
        return set(probed)
    trusted = set(bundled) | set(user)
    if platform == "darwin":
        return trusted
    return trusted | set(system)


def _dir_signature(dirs: list[Path]) -> list[list]:
    rows = []
    for directory in dirs:
        try:
            rows.append([str(directory), directory.stat().st_mtime_ns])
        except OSError:
            rows.append([str(directory), 0])
    return rows


def _cache_key(soffice: str | None, dirs: list[Path], candidates: set[str]) -> str:
    payload = {
        "version": CACHE_VERSION,
        "soffice": soffice or "",
        "dirs": _dir_signature(dirs),
        "candidates": sorted(candidates),
    }
    return json.dumps(payload, sort_keys=True)


def _disk_cache_path(key_str: str) -> Path:
    digest = hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"pptx_render_fonts_{digest}.json"


def load_font_cache(path: Path) -> dict | None:
    """Read a cache file. The pre-v2 format (no version) is ignored."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or "families" not in data:
        return None
    return data


def _probe_once(families: list[str], soffice: str, pdffonts: str) -> set[str] | None:
    """One probe page: each family on its own line. None when conversion fails."""
    if not families:
        return set()
    try:
        from compile_page_ir import compile_payload

        with tempfile.TemporaryDirectory(prefix="font-probe-") as tmp:
            root = Path(tmp)
            line_h = 18
            height = max(200, 24 + len(families) * line_h)
            objects = []
            for index, family in enumerate(families):
                objects.append({
                    "id": f"f{index}",
                    "type": "text",
                    "bbox": [12, 8 + index * line_h, 1600, line_h - 2],
                    "text": "中文Aa",
                    "style": {
                        "font_face": family,
                        "latin_font_face": family,
                        "font_size_pt": 12,
                        "color": "#111111",
                    },
                })
            payload = {
                "schema_version": "1.1",
                "target": {"platform": "mac", "installed_fonts": families},
                "pages": [{
                    "id": "probe",
                    "width_px": 1800,
                    "height_px": height,
                    "objects": objects,
                }],
            }
            pptx = root / "probe.pptx"
            compile_payload(payload, root, pptx)
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(root), str(pptx)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=90,
            )
            pdf = root / "probe.pdf"
            if proc.returncode != 0 or not pdf.exists():
                return None
            listed = subprocess.run([pdffonts, str(pdf)], text=True, stdout=subprocess.PIPE, check=False, timeout=20)
            if listed.returncode != 0:
                return None
            return match_probed_families(set(families), listed.stdout)
    except Exception:
        return None


def _probe_families(families: set[str], soffice: str | None) -> set[str] | None:
    pdffonts = shutil.which("pdffonts")
    if not soffice or not pdffonts or not families:
        return set() if families == set() and soffice and pdffonts else None
    ordered = sorted(families)
    whole = _probe_once(ordered, soffice, pdffonts)
    if whole is not None:
        return whole
    if len(ordered) <= 40:
        return None
    confirmed: set[str] = set()
    ok = False
    for start in range(0, len(ordered), 40):
        part = _probe_once(ordered[start:start + 40], soffice, pdffonts)
        if part is None:
            continue
        ok = True
        confirmed |= part
    return confirmed if ok else None


def _scan_groups(soffice: str | None) -> tuple[list[Path], set[str], set[str], set[str]]:
    bundled_dirs = libreoffice_font_dirs(soffice)
    user_dirs = user_font_dirs()
    system_dirs = [path for path in SYSTEM_FONT_DIRS if path.exists()]
    bundled = scan_font_dirs(tuple(bundled_dirs))
    user = scan_font_dirs(tuple(user_dirs))
    system = scan_font_dirs(tuple(system_dirs))
    return bundled_dirs + user_dirs + system_dirs, bundled, user, system


def detect_render_fonts(extra_families: set[str] | None = None) -> dict:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    platform = "darwin" if sys.platform == "darwin" else "linux"
    fc = _fc_list()
    extras = {name for name in (extra_families or set()) | style_candidate_names() if name and not is_theme_font(name)}
    dirs, bundled, user, system = _scan_groups(soffice)
    candidates = set(bundled) | set(user) | set(system) | set(fc or set()) | extras
    key = _cache_key(soffice, dirs, candidates)
    if key in _CACHE:
        return _CACHE[key]
    cache_file = _disk_cache_path(key)
    if cache_file.exists():
        cached = load_font_cache(cache_file)
        if cached is not None:
            _CACHE[key] = cached
            return cached

    if fc is not None:
        families = select_drawable(
            platform=platform, fontconfig=fc, probed=None, bundled=bundled, user=user, system=system,
        )
        report = {
            "version": CACHE_VERSION,
            "families": sorted(families),
            "fontconfig": True,
            "soffice": soffice,
            "confirmed": True,
            "note": "families taken from fontconfig",
        }
    else:
        probed = _probe_families(candidates, soffice)
        families = select_drawable(
            platform=platform, fontconfig=None, probed=probed, bundled=bundled, user=user, system=system,
        )
        if probed is not None:
            note = "families confirmed by LibreOffice probe and pdffonts"
        elif platform == "darwin":
            note = "LibreOffice probe failed; only bundled and user fonts count as drawable"
        else:
            note = "LibreOffice probe failed; using bundled, user, and system families"
        report = {
            "version": CACHE_VERSION,
            "families": sorted(families),
            "fontconfig": False,
            "soffice": soffice,
            "confirmed": probed is not None,
            "note": note,
        }
    _CACHE[key] = report
    try:
        cache_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return report


def drawable_families(extra_families: set[str] | None = None) -> set[str]:
    return {str(name) for name in detect_render_fonts(extra_families)["families"]}


def closest_drawable(name: str, style: str | None = None, families: set[str] | None = None) -> str | None:
    available = families if families is not None else drawable_families()
    lowered = {item.lower(): item for item in available}
    # A face LibreOffice actually embedded is used as itself. Delivery faces
    # that only exist on the OS (Helvetica Neue, PingFang, ...) are not in
    # `available`, so they fall through to the substitute list.
    if name.lower() in lowered:
        return lowered[name.lower()]
    style = style or infer_style(name) or (
        "latin_sans" if is_latin_font(name) or (is_theme_font(name) and str(name).endswith("-lt")) else "sans"
    )
    for candidate in STYLE_CANDIDATES.get(style, []):
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return next(iter(available), None)


def _metric_compatible(requested: str, rendered: str | None) -> bool:
    """Arial and Liberation Sans share metrics. Helvetica does not."""
    if not rendered or requested.startswith("+mn-") or requested.startswith("+mj-"):
        return False
    pair = {requested.lower(), rendered.lower()}
    return pair <= {"arial", "liberation sans"}


def _payload_font_names(payload: dict) -> set[str]:
    names: set[str] = set()
    platform = str((payload.get("target") or {}).get("platform") or "any").lower()
    default_face = payload.get("default_font_face")
    if default_face:
        names.add(str(default_face))
    defaults = DEFAULT_FONTS.get(platform, DEFAULT_FONTS["any"])
    names.add(defaults["cjk"])
    names.add(defaults["latin"])
    for page in payload.get("pages", []):
        for obj in page.get("objects", []):
            if obj.get("type") != "text":
                continue
            style = obj.get("style") or {}
            for key in ("font_face", "latin_font_face"):
                if style.get(key):
                    names.add(str(style[key]))
            for run in obj.get("runs") or []:
                if run.get("font_face"):
                    names.add(str(run["font_face"]))
                if run.get("latin_font_face"):
                    names.add(str(run["latin_font_face"]))
    return {name for name in names if name and not is_theme_font(name)}


def substitutions_for_payload(payload: dict) -> list[dict]:
    names = _payload_font_names(payload)
    platform = str((payload.get("target") or {}).get("platform") or "any").lower()
    defaults = DEFAULT_FONTS.get(platform, DEFAULT_FONTS["any"])
    # Theme refs are not probe candidates, but they still need a substitute.
    theme_names = {defaults["cjk"], defaults["latin"]}
    for page in payload.get("pages", []):
        for obj in page.get("objects", []):
            if obj.get("type") != "text":
                continue
            style = obj.get("style") or {}
            for key in ("font_face", "latin_font_face"):
                if is_theme_font(style.get(key)):
                    theme_names.add(str(style[key]))
            for run in obj.get("runs") or []:
                for key in ("font_face", "latin_font_face"):
                    if is_theme_font(run.get(key)):
                        theme_names.add(str(run[key]))
    families = drawable_families(names)
    result = []
    for requested in sorted(names | theme_names):
        style = "latin_sans" if requested.endswith("-lt") or is_latin_font(requested) else infer_style(requested) or "sans"
        rendered = closest_drawable(requested, style, families)
        substituted = is_theme_font(requested) or (rendered is not None and rendered.lower() != requested.lower())
        result.append({
            "requested": requested,
            "rendered_with": rendered,
            "substituted": substituted,
            "metric_compatible": _metric_compatible(requested, rendered) and substituted,
        })
    return result


def substitute_payload(payload: dict) -> tuple[dict, list[dict]]:
    substitutions = substitutions_for_payload(payload)
    mapping = {item["requested"]: item["rendered_with"] for item in substitutions if item["rendered_with"]}
    out = copy.deepcopy(payload)
    platform = str((out.get("target") or {}).get("platform") or "any").lower()
    default_cjk = DEFAULT_FONTS.get(platform, DEFAULT_FONTS["any"])["cjk"]
    default_latin = DEFAULT_FONTS.get(platform, DEFAULT_FONTS["any"])["latin"]

    for page in out.get("pages", []):
        for obj in page.get("objects", []):
            if obj.get("type") != "text":
                continue
            style = obj.setdefault("style", {})
            face = style.get("font_face") or default_cjk
            style["font_face"] = mapping.get(face, face)
            latin_face = style.get("latin_font_face") or (face if is_latin_font(face) else default_latin)
            style["latin_font_face"] = mapping.get(latin_face, latin_face)
            for run in obj.get("runs") or []:
                run_face = run.get("font_face")
                if run_face:
                    run["font_face"] = mapping.get(run_face, run_face)
                run_latin = run.get("latin_font_face")
                if run_latin:
                    run["latin_font_face"] = mapping.get(run_latin, run_latin)
    return out, substitutions
