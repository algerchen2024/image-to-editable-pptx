#!/usr/bin/env python3
"""Create dist/skill.zip after validating the public skill repository."""

from __future__ import annotations

import argparse
import importlib.util
import zipfile
from pathlib import Path


MAX_BYTES = 25 * 1024 * 1024
RUNTIME_TOP_LEVEL = [
    "SKILL.md",
    "agents",
    "assets",
    "references",
    "scripts",
    "requirements.txt",
    "package.json",
    "package-lock.json",
]


def load_validator(root: Path):
    module_path = root / "scripts" / "validate_skill.py"
    spec = importlib.util.spec_from_file_location("skill_validator", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def iter_runtime_files(root: Path):
    for entry in RUNTIME_TOP_LEVEL:
        path = root / entry
        if not path.exists():
            continue
        if path.is_file():
            yield path
        else:
            for child in sorted(path.rglob("*")):
                if child.is_file() and "__pycache__" not in child.parts and not child.name.endswith(".pyc"):
                    yield child


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--outdir", default="dist")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    validator = load_validator(root)
    errors = validator.validate(root)
    if errors:
        for item in errors:
            print(f"ERROR: {item}")
        return 2
    outdir = Path(args.outdir)
    if not outdir.is_absolute():
        outdir = root / outdir
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / "skill.zip"
    skill_name = root.name
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in iter_runtime_files(root):
            arcname = Path(skill_name) / file_path.relative_to(root)
            archive.write(file_path, arcname.as_posix())
    size = output.stat().st_size
    if size > MAX_BYTES:
        output.unlink(missing_ok=True)
        print(f"ERROR: skill.zip exceeds 25 MiB ({size} bytes)")
        return 3
    print(f"{output} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
