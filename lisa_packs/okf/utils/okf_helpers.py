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
    generated_at: str = "",
    resource: str = "",
    stale_after: str = "",
    sources: list[dict[str, object]] | None = None,
    verified: list[dict[str, object]] | None = None,
) -> str:
    """Build the YAML frontmatter block for an OKF concept document.

    Args:
        concept_type: The concept type (required OKF field).
        title: Human-readable display name.
        description: One-line summary.
        tags: Categorization tags.
        status: Lifecycle status (draft, stable, deprecated).
        generated_by: Actor string for ``generated.by``.
        generated_at: Override for ``generated.at`` (ISO 8601). Defaults to now.
        resource: Canonical URI for the underlying asset.
        stale_after: YYYY-MM-DD date after which the concept is stale.
        sources: List of source mappings (``{id, resource, title, ...}``).
        verified: List of verification mappings (``{by, at}``).
    """
    now = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

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
    if resource:
        fm["resource"] = resource
    if stale_after:
        fm["stale_after"] = stale_after
    if sources:
        fm["sources"] = sources
    if verified:
        fm["verified"] = verified

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


# ---------------------------------------------------------------------------
# Bundle-level helpers (index.md + log.md — OKF v0.2 §3.1)
# ---------------------------------------------------------------------------


def build_index_markdown(concepts: list[dict[str, object]]) -> str:
    """Build the body of an OKF ``index.md`` directory listing.

    Args:
        concepts: list of dicts with at least ``path`` and ``type`` keys;
            ``title`` and ``description`` are included when present.

    Returns:
        Markdown body (without frontmatter) for ``index.md``.
    """
    lines: list[str] = [
        "# Bundle Index",
        "",
        f"{len(concepts)} concept{'s' if len(concepts) != 1 else ''}.",
        "",
        "| Path | Type | Title | Description |",
        "| --- | --- | --- | --- |",
    ]
    for concept in sorted(concepts, key=lambda c: str(c.get("path", ""))):
        path = str(concept.get("path", ""))
        ctype = str(concept.get("type", ""))
        title = str(concept.get("title", "") or "")
        desc = str(concept.get("description", "") or "")
        lines.append(f"| `{path}` | {ctype} | {title} | {desc} |")
    lines.append("")
    return "\n".join(lines)


def build_log_entry(
    *,
    action: str,
    concept_path: str = "",
    by: str = "",
    note: str = "",
) -> str:
    """Format a single append-only line for OKF ``log.md``.

    Args:
        action: short verb (``emit``, ``verify``, ``deprecate``, ``scaffold``...).
        concept_path: relative path of the affected concept, or empty for bundle-level.
        by: actor string (``human:adam``, ``lisa-pack-okf/0.1.0``...).
        note: free-form one-line note.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"- {now} {action}"]
    if concept_path:
        parts.append(f"`{concept_path}`")
    if by:
        parts.append(f"by {by}")
    if note:
        parts.append(f"— {note}")
    return " ".join(parts)


def append_log_entry(bundle: Path, entry: str) -> Path:
    """Append a line to the bundle's ``log.md``, creating the file if needed.

    Returns the path to ``log.md``.
    """
    log_path = bundle / "log.md"
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")
        # Keep a trailing newline so append lands on its own line
        if not existing.endswith("\n"):
            existing += "\n"
        log_path.write_text(existing + entry + "\n", encoding="utf-8")
    else:
        log_path.write_text(
            f"# Bundle Log\n\n{entry}\n",
            encoding="utf-8",
        )
    return log_path
