"""OKF ingest operation — read an OKF v0.2 bundle and return its concepts.

No-LLM deterministic read: walks a directory tree, parses YAML frontmatter
from every non-reserved .md file, and returns structured concept documents.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult

logger = logging.getLogger(__name__)

_RESERVED_FILENAMES = frozenset({"index.md", "log.md"})


class Parameters(BaseModel):
    """User-supplied inputs for the OKF ingest operation."""

    bundle_path: str = Field(
        ...,
        description="Absolute path to the OKF bundle directory to ingest.",
    )


class OkfConcept(BaseModel):
    """A single concept document from an OKF bundle."""

    path: str = Field(..., description="Relative path of the concept within the bundle.")
    type: str = Field(..., description="The concept type (required OKF field).")
    title: str = Field(default="", description="Human-readable display name.")
    description: str = Field(default="", description="One-line summary.")
    resource: str = Field(default="", description="Canonical URI for the underlying asset.")
    tags: list[str] = Field(default_factory=list, description="Categorization tags.")
    status: str = Field(default="stable", description="Lifecycle status: draft, stable, or deprecated.")
    generated_by: str = Field(default="", description="Actor who produced the content.")
    generated_at: str = Field(default="", description="ISO 8601 timestamp of generation.")
    body: str = Field(default="", description="Markdown body content (first 2000 chars).")


class OkfBundle(BaseModel):
    """A complete OKF bundle ingested from disk."""

    bundle_path: str = Field(..., description="The bundle directory path.")
    concepts: list[OkfConcept] = Field(default_factory=list, description="All concept documents in the bundle.")


def _parse_frontmatter(file_path: Path) -> dict[str, object] | None:
    """Parse YAML frontmatter from a markdown file."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not text.startswith("---"):
        return None

    end_idx = text.find("---", 3)
    if end_idx == -1:
        return None

    frontmatter_text = text[3:end_idx].strip()
    if not frontmatter_text:
        return None

    try:
        parsed = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed


def _extract_body(text: str) -> str:
    """Extract the markdown body after the frontmatter block."""
    if not text.startswith("---"):
        return text.strip()

    end_idx = text.find("---", 3)
    if end_idx == -1:
        return text.strip()

    # Find the end of the closing --- line
    after_close = text[end_idx + 3:]
    next_newline = after_close.find("\n")
    if next_newline != -1:
        return after_close[next_newline + 1:].strip()
    return after_close.strip()


def _str_value(data: dict[str, object], key: str) -> str:
    """Safely extract a string value from a dict."""
    val = data.get(key)
    if isinstance(val, str):
        return val
    return ""


def _generated_info(data: dict[str, object]) -> tuple[str, str]:
    """Extract generated.by and generated.at from frontmatter.

    OKF v0.2 uses ``generated: { by, at }``. Also falls back to v0.1
    ``timestamp`` for ``generated_at``.
    """
    generated = data.get("generated")
    by = ""
    at = ""
    if isinstance(generated, dict):
        by = str(generated.get("by", ""))
        at = str(generated.get("at", ""))
    if not at:
        ts = data.get("timestamp")
        if isinstance(ts, str):
            at = ts
    return by, at


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Ingest an OKF v0.2 bundle and return its concepts as structured data."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    if not bundle_path_str:
        return OperationResult(
            output_text="OKF ingest failed: bundle_path is required.",
        )

    bundle = Path(bundle_path_str)
    if not bundle.is_dir():
        return OperationResult(
            output_text=f"OKF ingest failed: path is not a directory: {bundle_path_str}",
        )

    concepts: list[OkfConcept] = []

    for md_file in sorted(bundle.rglob("*.md")):
        if md_file.name in _RESERVED_FILENAMES:
            continue

        rel_path = str(md_file.relative_to(bundle)).replace("\\", "/")
        frontmatter = _parse_frontmatter(md_file)

        if frontmatter is None:
            continue

        type_val = _str_value(frontmatter, "type")
        if not type_val:
            continue

        try:
            full_text = md_file.read_text(encoding="utf-8")
        except OSError:
            full_text = ""

        body = _extract_body(full_text)
        # Truncate body for structured output — full body is available on disk
        body_preview = body[:2000]

        generated_by, generated_at = _generated_info(frontmatter)

        tags_val = frontmatter.get("tags")
        tags: list[str] = []
        if isinstance(tags_val, list):
            tags = [str(t) for t in tags_val if t is not None]

        concepts.append(
            OkfConcept(
                path=rel_path,
                type=type_val,
                title=_str_value(frontmatter, "title"),
                description=_str_value(frontmatter, "description"),
                resource=_str_value(frontmatter, "resource"),
                tags=tags,
                status=_str_value(frontmatter, "status") or "stable",
                generated_by=generated_by,
                generated_at=generated_at,
                body=body_preview,
            )
        )

    bundle_data = OkfBundle(bundle_path=bundle_path_str, concepts=concepts)

    if not concepts:
        return OperationResult(
            output_text=f"OKF ingest: no concepts found in {bundle_path_str}",
            output_json=bundle_data.model_dump(),
        )

    type_counts: dict[str, int] = {}
    for c in concepts:
        type_counts[c.type] = type_counts.get(c.type, 0) + 1

    summary_lines = [
        f"OKF bundle ingested: {bundle_path_str}",
        f"Concepts: {len(concepts)}",
        "",
        "By type:",
    ]
    for t, count in sorted(type_counts.items()):
        summary_lines.append(f"  {t}: {count}")

    return OperationResult(
        output_text="\n".join(summary_lines),
        output_json=bundle_data.model_dump(),
    )
