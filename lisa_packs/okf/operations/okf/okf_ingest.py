"""OKF ingest operation — read an OKF v0.2 bundle and return its concepts.

No-LLM deterministic read: walks the bundle via ``lisa-storage``
(``list_files`` + ``read_file``), parses YAML frontmatter from every
non-reserved ``.md`` file, and returns structured concept documents.
Every read goes through ``lisa-storage``; ``bundle_path`` is a
sandbox-relative path under ``data_dir/files`` (e.g. ``okf/my-bundle``).
"""

from __future__ import annotations

import logging

from kernel.models import OperationExecutionContext, OperationResult
from pydantic import BaseModel, Field

from lisa_packs.okf.utils.okf_helpers import (
    RESERVED_FILENAMES,
    list_bundle_files,
    parse_frontmatter,
    read_bundle_file,
)

logger = logging.getLogger(__name__)

#: Number of leading characters of a concept body kept in the structured
#: output when body truncation is enabled.
_BODY_PREVIEW_CHARS = 2000


class Parameters(BaseModel):
    """User-supplied inputs for the OKF ingest operation."""

    bundle_path: str = Field(
        ...,
        description="Relative path within the lisa-storage files sandbox for the OKF "
        "bundle directory to ingest.",
    )
    truncate_body: bool = Field(
        default=True,
        description="If True (default), truncate each concept body to 2000 chars in the "
        "structured output. Set False to receive the full body text.",
    )


class OkfConcept(BaseModel):
    """A single concept document from an OKF bundle."""

    path: str = Field(..., description="Relative path of the concept within the bundle.")
    type: str = Field(..., description="The concept type (required OKF field).")
    title: str = Field(default="", description="Human-readable display name.")
    description: str = Field(default="", description="One-line summary.")
    resource: str = Field(default="", description="Canonical URI for the underlying asset.")
    tags: list[str] = Field(default_factory=list, description="Categorization tags.")
    status: str = Field(
        default="stable", description="Lifecycle status: draft, stable, or deprecated."
    )
    generated_by: str = Field(default="", description="Actor who produced the content.")
    generated_at: str = Field(default="", description="ISO 8601 timestamp of generation.")
    body: str = Field(default="", description="Markdown body content (first 2000 chars).")


class OkfBundle(BaseModel):
    """A complete OKF bundle ingested from the storage sandbox."""

    bundle_path: str = Field(..., description="The bundle directory path.")
    concepts: list[OkfConcept] = Field(
        default_factory=list, description="All concept documents in the bundle."
    )


def _extract_body(text: str) -> str:
    """Extract the markdown body after the frontmatter block."""
    if not text.startswith("---"):
        return text.strip()

    end_idx = text.find("---", 3)
    if end_idx == -1:
        return text.strip()

    # Find the end of the closing --- line
    after_close = text[end_idx + 3 :]
    next_newline = after_close.find("\n")
    if next_newline != -1:
        return after_close[next_newline + 1 :].strip()
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


def _strip_bundle_prefix(sandbox_path: str, bundle_path_str: str) -> str:
    """Strip the bundle's sandbox prefix to get the in-bundle relative path."""
    p = sandbox_path.replace("\\", "/")
    prefix = bundle_path_str.replace("\\", "/").strip("/")
    if prefix and p.startswith(prefix + "/"):
        return p[len(prefix) + 1 :]
    return p


def _frontmatter_tags(frontmatter: dict[str, object]) -> list[str]:
    """Extract the ``tags`` list from a frontmatter block."""
    tags_val = frontmatter.get("tags")
    if not isinstance(tags_val, list):
        return []
    return [str(t) for t in tags_val if t is not None]


def _build_concept(
    rel_path: str,
    text: str,
    frontmatter: dict[str, object],
    *,
    truncate: bool,
) -> OkfConcept:
    """Build one concept document from its file text and frontmatter."""
    body = _extract_body(text)
    # Truncation is opt-in via the ``truncate_body`` parameter so that
    # callers who want the full text (e.g. a backend reading concepts)
    # get it. Default remains 2000 for backwards compatibility with
    # chat-mode summaries where token budget matters.
    body_preview = body[:_BODY_PREVIEW_CHARS] if truncate else body
    generated_by, generated_at = _generated_info(frontmatter)
    return OkfConcept(
        path=rel_path,
        type=_str_value(frontmatter, "type"),
        title=_str_value(frontmatter, "title"),
        description=_str_value(frontmatter, "description"),
        resource=_str_value(frontmatter, "resource"),
        tags=_frontmatter_tags(frontmatter),
        status=_str_value(frontmatter, "status") or "stable",
        generated_by=generated_by,
        generated_at=generated_at,
        body=body_preview,
    )


async def _ingest_concepts(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    *,
    truncate: bool,
) -> list[OkfConcept]:
    """Read a bundle and return every concept document it contains."""
    files = await list_bundle_files(ctx, bundle_path_str)
    concepts: list[OkfConcept] = []

    for sandbox_path in files:
        rel_path = _strip_bundle_prefix(sandbox_path, bundle_path_str)
        if not rel_path.endswith(".md"):
            continue
        if rel_path.rsplit("/", 1)[-1] in RESERVED_FILENAMES:
            continue

        text = await read_bundle_file(ctx, bundle_path_str, rel_path)
        if text is None:
            continue

        frontmatter = parse_frontmatter(text)
        if frontmatter is None:
            continue

        type_val = _str_value(frontmatter, "type")
        if not type_val:
            continue

        concepts.append(
            _build_concept(rel_path, text, frontmatter, truncate=truncate),
        )

    return concepts


def _summarize(concepts: list[OkfConcept], bundle_path_str: str) -> str:
    """Build the human-readable ingest summary."""
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
    return "\n".join(summary_lines)


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Ingest an OKF v0.2 bundle and return its concepts as structured data."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip().strip("/")
    if not bundle_path_str:
        return OperationResult(
            output_text="OKF ingest failed: bundle_path is required.",
        )

    if ".." in bundle_path_str.replace("\\", "/").split("/"):
        return OperationResult(
            output_text="OKF ingest failed: bundle_path may not contain '..' segments.",
        )

    if ctx.host is None:
        return OperationResult(
            output_text=(
                "OKF ingest failed: tool execution not injected (lisa-storage unavailable)."
            ),
        )

    truncate = bool(ctx.params.get("truncate_body", True))
    concepts = await _ingest_concepts(ctx, bundle_path_str, truncate=truncate)

    bundle_data = OkfBundle(bundle_path=bundle_path_str, concepts=concepts)

    if not concepts:
        return OperationResult(
            output_text=f"OKF ingest: no concepts found in {bundle_path_str}",
            output_json=bundle_data.model_dump(),
        )

    return OperationResult(
        output_text=_summarize(concepts, bundle_path_str),
        output_json=bundle_data.model_dump(),
    )
