#!/usr/bin/env python3
"""Portable structural validator for this ChatGPT Skill repository."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml


NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DISPLAY_VERSION_RE = re.compile(r"\bV(\d+)(?:\.(\d+))?$")


def check_display_version(root: Path, display: str) -> list[str]:
    """The V<major>[.<minor>] suffix users see in ChatGPT must match package.json."""
    package = root / "package.json"
    if not package.exists():
        return []
    version = str(json.loads(package.read_text(encoding="utf-8")).get("version", ""))
    parts = version.split(".")
    major = parts[0]
    minor = parts[1] if len(parts) > 1 else "0"
    expected = f"V{major}" if minor == "0" else f"V{major}.{minor}"
    match = DISPLAY_VERSION_RE.search(display.strip())
    if not match:
        return [f"agents/openai.yaml display_name must end with {expected} to match package.json {version}"]
    shown_minor = match.group(2) or "0"
    if match.group(1) != major or shown_minor != minor:
        return [f"display_name says {match.group(0)} but package.json version {version} needs {expected}"]
    return []


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        return ["SKILL.md is missing"]
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        errors.append("SKILL.md must begin with YAML frontmatter")
        return errors
    parts = text.split("---", 2)
    if len(parts) < 3:
        errors.append("SKILL.md frontmatter is not closed")
        return errors
    try:
        frontmatter = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        errors.append(f"invalid YAML frontmatter: {exc}")
        return errors
    if set(frontmatter) != {"name", "description"}:
        errors.append("SKILL.md frontmatter must contain only name and description")
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append("skill name must be lowercase kebab-case")
    if not isinstance(description, str) or len(description.strip()) < 80:
        errors.append("description must clearly describe capability and triggers")
    if "TODO" in text:
        errors.append("SKILL.md contains TODO placeholder text")
    agent = root / "agents" / "openai.yaml"
    if not agent.exists():
        errors.append("agents/openai.yaml is missing")
    else:
        try:
            payload = yaml.safe_load(agent.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            errors.append(f"invalid agents/openai.yaml: {exc}")
        else:
            display = ((payload.get("interface") or {}).get("display_name"))
            if not isinstance(display, str) or not display.strip():
                errors.append("agents/openai.yaml interface.display_name is missing")
            else:
                errors.extend(check_display_version(root, display))
    for relative in [
        "scripts/validate_page_ir.py",
        "scripts/compile_page_ir.js",
        "references/pageir-schema.md",
        "references/quality-checks.md",
    ]:
        if not (root / relative).exists():
            errors.append(f"required repository file is missing: {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    errors = validate(root)
    result = {"valid": not errors, "errors": errors}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif errors:
        print("Skill validation failed:")
        for error in errors:
            print(f"- {error}")
    else:
        print("Skill validation passed")
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
