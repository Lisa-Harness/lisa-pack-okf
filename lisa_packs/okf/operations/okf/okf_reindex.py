"""OKF reindex operation — regenerate a bundle's ``index.md``.

No-LLM deterministic write: walks the bundle, parses frontmatter from
every non-reserved ``.md`` file, and rewrites ``index.md`` with a
current table of contents. Appends an entry to ``log.md``.

Operates on the local filesystem (``pathlib``) — the same model as
``okf_validate`` and ``okf_ingest``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult
from lisa_packs.okf.utils.okf_helpers import (
    append_log_entry,
    build_index_markdown,
    build_log_entry,
    format_okf_frontmatter,
)

logger = logging.getLogger(__name__)

_RESERVED_FILENAMES = frozenset({"index.md", "log.md"})


class Parameters(BaseModel):
    """User-supplied inputs for the OKF reindex operation."""

    bundle_path: str = Field(
        ...,
        description="Absolute path to the OKF bundle directory to reindex.",
    )
    title: str = Field(
        default="",
        description="Override title for index.md. If empty, keeps the existing index title or uses the bundle name.",
    )


def _parse_frontmatter(file_path: Path) -> dict[str, object] | None:
    """Parse YAML frontmatter from a markdown file (best-effort)."""
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
    return parsed if isinstance(parsed, dict) else None


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Regenerate ``index.md`` for an OKF bundle from its current concepts."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    if not bundle_path_str:
        return OperationResult(output_text="OKF reindex failed: bundle_path is required.")

    title_override = str(ctx.params.get("title", "")).strip()

    bundle = Path(bundle_path_str)
    if not bundle.is_dir():
        return OperationResult(
            output_text=f"OKF reindex failed: path is not a directory: {bundle_path_str}",
        )

    # Preserve the existing index title if present and no override given
    existing_title = ""
    index_file = bundle / "index.md"
    if index_file.exists() and not title_override:
        fm = _parse_frontmatter(index_file)
        if fm:
            existing_title = str(fm.get("title", "") or "")

    title = title_override or existing_title or bundle.name

    concepts: list[dict[str, object]] = []
    for md_file in sorted(bundle.rglob("*.md")):
        if md_file.name in _RESERVED_FILENAMES:
            continue
        fm = _parse_frontmatter(md_file)
        if fm is None:
            continue
        if not str(fm.get("type", "")).strip():
            continue
        rel = str(md_file.relative_to(bundle)).replace("\\", "/")
        concepts.append(
            {
                "path": rel,
                "type": str(fm.get("type", "")),
                "title": str(fm.get("title", "") or ""),
                "description": str(fm.get("description", "") or ""),
            }
        )

    index_fm = format_okf_frontmatter(
        concept_type="OKF Bundle Index",
        title=title,
    )
    index_body = build_index_markdown(concepts)
    index_file.write_text(index_fm + "\n" + index_body, encoding="utf-8")

    log_entry = build_log_entry(
        action="reindex",
        by="lisa-pack-okf/0.1.0",
        note=f"{len(concepts)} concepts",
    )
    append_log_entry(bundle, log_entry)

    return OperationResult(
        output_text=(
            f"OKF reindex complete: {bundle_path_str}\n"
            f"  index.md: regenerated ({len(concepts)} concepts)\n"
            f"  log.md: appended"
        ),
    )