"""OKF reindex operation — regenerate a bundle's ``index.md``.

No-LLM deterministic write: walks the bundle via ``lisa-storage``
(``list_files`` + ``read_file``), parses frontmatter from every
non-reserved ``.md`` file, and rewrites ``index.md`` with a current
table of contents via ``write_file``. Appends an entry to ``log.md``
(read-rewrite through storage).

Every read and write goes through ``lisa-storage``; ``bundle_path`` is
a sandbox-relative path under ``data_dir/files`` (e.g. ``okf/my-bundle``).
"""

from __future__ import annotations

import logging

from kernel.models import OperationExecutionContext, OperationResult
from pydantic import BaseModel, Field

from lisa_packs.okf.utils.okf_helpers import (
    RESERVED_FILENAMES,
    append_log_entry_storage,
    build_index_markdown,
    build_log_entry,
    format_okf_frontmatter,
    list_bundle_files,
    parse_frontmatter,
    read_bundle_file,
    write_bundle_file,
)

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF reindex operation."""

    bundle_path: str = Field(
        ...,
        description="Relative path within the lisa-storage files sandbox for the OKF "
        "bundle directory to reindex.",
    )
    title: str = Field(
        default="",
        description="Override title for index.md. If empty, keeps the existing "
        "index title or uses the bundle name.",
    )


def _strip_bundle_prefix(sandbox_path: str, bundle_path_str: str) -> str:
    """Strip the bundle's sandbox prefix to get the in-bundle relative path."""
    p = sandbox_path.replace("\\", "/")
    prefix = bundle_path_str.replace("\\", "/").strip("/")
    if prefix and p.startswith(prefix + "/"):
        return p[len(prefix) + 1 :]
    return p


async def _resolve_title(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    title_override: str,
    bundle_name: str,
) -> str:
    """Resolve the index title: override, else existing index title, else bundle name."""
    if title_override:
        return title_override
    # Preserve the existing index title if present and no override given.
    existing_index = await read_bundle_file(ctx, bundle_path_str, "index.md")
    if existing_index:
        fm = parse_frontmatter(existing_index)
        if fm:
            existing_title = str(fm.get("title", "") or "")
            if existing_title:
                return existing_title
    return bundle_name


async def _collect_concepts(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
) -> list[dict[str, object]]:
    """Walk the bundle via lisa-storage list_files + read_file and index its concepts."""
    files = await list_bundle_files(ctx, bundle_path_str)
    concepts: list[dict[str, object]] = []
    for sandbox_path in files:
        rel = _strip_bundle_prefix(sandbox_path, bundle_path_str)
        filename = rel.rsplit("/", 1)[-1]
        if filename in RESERVED_FILENAMES:
            continue
        if not rel.endswith(".md"):
            continue
        text = await read_bundle_file(ctx, bundle_path_str, rel)
        if text is None:
            continue
        fm = parse_frontmatter(text)
        if fm is None:
            continue
        if not str(fm.get("type", "")).strip():
            continue
        concepts.append(
            {
                "path": rel,
                "type": str(fm.get("type", "")),
                "title": str(fm.get("title", "") or ""),
                "description": str(fm.get("description", "") or ""),
            }
        )
    return concepts


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Regenerate ``index.md`` for an OKF bundle from its current concepts."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip().strip("/")
    if not bundle_path_str:
        return OperationResult(output_text="OKF reindex failed: bundle_path is required.")

    title_override = str(ctx.params.get("title", "")).strip()
    bundle_name = bundle_path_str.rsplit("/", 1)[-1]

    if ".." in bundle_path_str.replace("\\", "/").split("/"):
        return OperationResult(
            output_text="OKF reindex failed: bundle_path may not contain '..' segments.",
        )

    if ctx.host is None:
        return OperationResult(
            output_text=(
                "OKF reindex failed: tool execution not injected (lisa-storage unavailable)."
            ),
        )

    title = await _resolve_title(ctx, bundle_path_str, title_override, bundle_name)
    concepts = await _collect_concepts(ctx, bundle_path_str)

    index_fm = format_okf_frontmatter(
        concept_type="OKF Bundle Index",
        title=title,
    )
    index_body = build_index_markdown(concepts)
    if not await write_bundle_file(ctx, bundle_path_str, "index.md", index_fm + "\n" + index_body):
        return OperationResult(
            output_text="OKF reindex failed: could not write index.md via lisa-storage.",
        )

    log_entry = build_log_entry(
        action="reindex",
        by="lisa-pack-okf/0.1.0",
        note=f"{len(concepts)} concepts",
    )
    if not await append_log_entry_storage(ctx, bundle_path_str, log_entry):
        return OperationResult(
            output_text="OKF reindex failed: could not append to log.md via lisa-storage.",
        )

    return OperationResult(
        output_text=(
            f"OKF reindex complete: {bundle_path_str}\n"
            f"  index.md: regenerated ({len(concepts)} concepts)\n"
            f"  log.md: appended"
        ),
    )
