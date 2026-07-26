"""OKF emit concept operation — write a single OKF v0.2 concept document.

No-LLM deterministic write: takes concept metadata and body text, produces
a conformant OKF markdown file with YAML frontmatter. Creates parent
directories as needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult
from lisa_packs.okf.utils.okf_helpers import format_okf_frontmatter, write_concept_file

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF emit concept operation."""

    bundle_path: str = Field(
        ...,
        description="Absolute path to the OKF bundle directory to write into.",
    )
    concept_path: str = Field(
        ...,
        description="Relative path within the bundle for the concept file (e.g. 'modes/learn.md').",
    )
    type: str = Field(
        ...,
        description="The concept type (required OKF field).",
    )
    title: str = Field(
        default="",
        description="Human-readable display name.",
    )
    description: str = Field(
        default="",
        description="One-line summary of the concept.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Categorization tags.",
    )
    status: str = Field(
        default="stable",
        description="Lifecycle status: draft, stable, or deprecated.",
    )
    body: str = Field(
        default="",
        description="Markdown body content for the concept document.",
    )


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Write a single OKF v0.2 concept document to a bundle directory."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    concept_path_str = str(ctx.params.get("concept_path", "")).strip()
    type_str = str(ctx.params.get("type", "")).strip()
    title_str = str(ctx.params.get("title", "")).strip()
    description_str = str(ctx.params.get("description", "")).strip()
    body_str = str(ctx.params.get("body", "")).strip()

    tags_raw = ctx.params.get("tags", [])
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw if t is not None]
    else:
        tags = []

    status_str = str(ctx.params.get("status", "stable")).strip() or "stable"

    if not bundle_path_str:
        return OperationResult(output_text="OKF emit failed: bundle_path is required.")
    if not concept_path_str:
        return OperationResult(output_text="OKF emit failed: concept_path is required.")
    if not type_str:
        return OperationResult(output_text="OKF emit failed: type is required (OKF v0.2 §4.1).")

    # Validate concept_path doesn't use reserved filenames
    filename = Path(concept_path_str).name
    if filename in ("index.md", "log.md"):
        return OperationResult(
            output_text=f"OKF emit failed: '{filename}' is a reserved filename (OKF v0.2 §3.1).",
        )

    bundle = Path(bundle_path_str)

    frontmatter = format_okf_frontmatter(
        concept_type=type_str,
        title=title_str,
        description=description_str,
        tags=tags if tags else None,
        status=status_str,
    )

    try:
        concept_file = write_concept_file(bundle, concept_path_str, frontmatter, body_str)
    except OSError as exc:
        return OperationResult(
            output_text=f"OKF emit failed: could not write {concept_path_str}: {exc}",
        )

    rel_path = str(concept_file.relative_to(bundle)).replace("\\", "/")
    return OperationResult(
        output_text=(
            f"OKF concept emitted: {rel_path}\n"
            f"  type: {type_str}\n"
            f"  title: {title_str or '(untitled)'}\n"
            f"  bundle: {bundle_path_str}"
        ),
    )
