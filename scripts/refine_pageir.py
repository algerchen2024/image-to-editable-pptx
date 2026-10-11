#!/usr/bin/env python3
"""Apply the per-object corrections from render_compare.py metrics to a PageIR.

Example:
    render_compare.py source.png out.pptx --outdir quality --page-ir page_ir.json
    refine_pageir.py quality/metrics.json page_ir.json --out page_ir.json

Only objects whose fit status is "off" are touched. Text gets font-size,
letter-spacing, or position corrections (one kind per round); shapes, lines,
and images get their edges moved onto the source ink. Run the compare again
after refining; fidelity_loop.py automates the cycle.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from object_fit import refine_payload, substituted_fonts  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("metrics", help="metrics.json written by render_compare.py --page-ir")
    parser.add_argument("page_ir")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    payload = json.loads(Path(args.page_ir).read_text(encoding="utf-8"))
    if not any("object_fit" in s for s in metrics.get("slides", [])):
        parser.error("metrics.json has no object_fit data; run render_compare.py with --page-ir")
    refined, changed = refine_payload(payload, metrics["slides"], substituted_fonts(metrics))
    Path(args.out).write_text(json.dumps(refined, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": args.out, "changed": changed}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
