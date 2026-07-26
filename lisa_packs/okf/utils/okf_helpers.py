"""Shared OKF helpers for the OKF pack.

Extracted from okf_emit_concept so that okf_export_memory can reuse
the same frontmatter formatting and file-writing logic without
duplicating code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml


def format_okf_frontmatter(
    *,
    concept_type: str,
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
    status: str = "stable",
    generated_by: str = "lisa-pack-okf/0.1.0",
) -> str:
    """Build the YAML frontmatter block for an OKF concept document.

    Args:
        concept_type: The concept type (required OKF field).
        title: Human-readable display name.
        description: One-line summary.
        tags: Categorization tags.
        status: Lifecycle status (draft, stable, deprecated).
        generated_by: Actor string for ``generated.by``.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    fm: dict[str, object] = {
        "type": concept_type,
        "generated": {"by": generated_by, "at": now},
    }

    if title:
        fm["title"] = title
    if description:
        fm["description"] = description
    if tags:
        fm["tags"] = tags
    if status and status != "stable":
        fm["status"] = status

    yaml_text = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{yaml_text}---\n"


def write_concept_file(
    bundle: Path,
    concept_path: str,
    frontmatter: str,
    body: str = "",
) -> Path:
    """Write a single OKF concept document to a bundle directory.

    Creates parent directories as needed. Returns the written file path.

    Args:
        bundle: The bundle root directory.
        concept_path: Relative path within the bundle (e.g. ``modes/learn.md``).
        frontmatter: The YAML frontmatter block (including ``---`` delimiters).
        body: The markdown body content.
    """
    if not concept_path.endswith(".md"):
        concept_path = concept_path + ".md"

    concept_file = bundle / concept_path
    concept_file.parent.mkdir(parents=True, exist_ok=True)

    content = frontmatter
    if body:
        content += f"\n{body}\n"

    concept_file.write_text(content, encoding="utf-8")
    return concept_file


def sanitize_filename(name: str) -> str:
    """Convert a string into a safe filename component.

    Replaces spaces and special characters with hyphens, collapses
    runs, and strips leading/trailing separators.
    """
    import re

    safe = re.sub(r"[^a-z0-9]+", "-", name.lower())
    safe = safe.strip("-")
    return safe or "untitled"
