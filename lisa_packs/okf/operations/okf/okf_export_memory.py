"""OKF export memory operation — export memory records to an OKF v0.2 bundle.

No-LLM deterministic export: calls ``memory.list`` to retrieve records,
then writes each as an OKF concept document through the ``lisa-storage``
MCP server's ``write_file`` tool — never touches the filesystem directly.
"""

from __future__ import annotations

import json
import logging

from kernel.models import OperationExecutionContext, OperationResult, ToolExecutionContext
from pydantic import BaseModel, Field

from lisa_packs.okf.utils.okf_helpers import (
    format_okf_frontmatter,
    sanitize_filename,
)

logger = logging.getLogger(__name__)

#: Default number of memory records to export when the caller does not
#: set a limit.
_DEFAULT_LIMIT = 100

#: Hard ceiling on the number of memory records exported in one run.
_MAX_LIMIT = 1000

#: Number of leading characters of a record's content used as the
#: concept title.
_TITLE_CHARS = 60

#: Number of leading characters of a record's content used as the
#: concept description.
_DESCRIPTION_CHARS = 120

#: Number of leading characters of the title used when a record has no
#: id to derive a filename from.
_FILENAME_TITLE_CHARS = 40

#: Memory tiers that carry no signal as an OKF tag.
_UNTAGGED_MEMORY_TIERS = frozenset({"daily"})

#: Operation ids that are plumbing rather than provenance, so they are
#: not emitted as OKF tags.
_UNTAGGED_OPERATION_IDS = frozenset({"mcp.add_memory", "mcp.v1"})


class Parameters(BaseModel):
    """User-supplied inputs for the OKF export memory operation."""

    bundle_path: str = Field(
        ...,
        description="Relative path within the lisa-storage files sandbox for the OKF "
        "bundle directory (e.g. 'okf/my-bundle'). Created if it doesn't exist.",
    )
    limit: int = Field(
        default=_DEFAULT_LIMIT,
        description="Maximum number of memory records to export.",
        ge=1,
        le=_MAX_LIMIT,
    )


def _record_title(content: str) -> str:
    """Derive a concept title from a record's content."""
    title = content[:_TITLE_CHARS].replace("\n", " ").strip()
    if len(content) > _TITLE_CHARS:
        title += "…"
    return title


def _record_tags(record: dict[str, object]) -> list[str]:
    """Derive OKF tags from a memory record's metadata."""
    tags: list[str] = []
    tier = str(record.get("memory_tier", ""))
    if tier and tier not in _UNTAGGED_MEMORY_TIERS:
        tags.append(tier)
    op_id = str(record.get("operation_id", ""))
    if op_id and op_id not in _UNTAGGED_OPERATION_IDS:
        tags.append(op_id.replace("_", "-"))
    return tags


def _record_filename(record: dict[str, object], title: str) -> str:
    """Build a safe concept filename from a record's id, or its title."""
    record_id = str(record.get("id", ""))
    if record_id:
        return f"{sanitize_filename(record_id)}.md"
    return f"{sanitize_filename(title[:_FILENAME_TITLE_CHARS])}.md"


def _record_body(record: dict[str, object], title: str, content: str, tier: str) -> str:
    """Build the markdown body of an exported memory record."""
    body_lines = [f"# {title}", "", content, ""]

    created_at = str(record.get("created_at", ""))
    salience = record.get("salience")
    if created_at or salience is not None or tier:
        body_lines.append("## Metadata")
        body_lines.append("")
        body_lines.append("| Field | Value |")
        body_lines.append("| --- | --- |")
        if created_at:
            body_lines.append(f"| Created | {created_at} |")
        if salience is not None:
            body_lines.append(f"| Salience | {salience} |")
        if tier:
            body_lines.append(f"| Tier | {tier} |")
        body_lines.append("")

    return "\n".join(body_lines)


def _parse_records(output_text: str) -> list[object]:
    """Parse the ``memory.list`` payload into the raw list of records.

    Non-mapping entries are kept in the list (they are counted as
    skipped by the caller, and are part of the reported total), so the
    returned list mirrors the payload one-for-one.
    """
    try:
        data: object = json.loads(output_text or "[]")
    except (json.JSONDecodeError, TypeError):
        data = []

    if isinstance(data, dict) and "results" in data:
        results = data.get("results")
        if isinstance(results, list):
            data = results

    if not isinstance(data, list):
        return []
    return list(data)


async def _write_record(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    record: dict[str, object],
) -> bool:
    """Write one memory record as an OKF concept document.

    Returns ``True`` on a successful write, ``False`` on a storage error
    (the caller counts it as skipped).
    """
    content = str(record.get("content", "")).strip()
    if not content:
        return False

    title = _record_title(content)
    tags = _record_tags(record)
    tier = str(record.get("memory_tier", ""))
    filename = _record_filename(record, title)
    concept_path = f"memories/{filename}"
    body = _record_body(record, title, content, tier)

    frontmatter = format_okf_frontmatter(
        concept_type="Memory Record",
        title=title,
        description=content[:_DESCRIPTION_CHARS].replace("\n", " ").strip(),
        tags=tags if tags else None,
    )

    full_content = frontmatter + f"\n{body}\n"
    full_path = f"{bundle_path_str}/{concept_path}"

    try:
        write_result = await ctx.host.execute_tool(
            "write_file",
            ToolExecutionContext(
                tool_id="write_file",
                workspace_id=ctx.workspace_id,
                user_id=ctx.user_id,
                user_slug=ctx.user_slug,
                surface_id=ctx.surface_id,
                memory_scope=ctx.memory_scope,
                mode_id=ctx.mode_id,
                parameters={"path": full_path, "content": full_content},
            ),
        )
        result_data = json.loads(write_result.output_text or "{}")
        if result_data.get("ok"):
            return True
        logger.warning(
            "Failed to write OKF concept %s: %s",
            concept_path,
            result_data.get("error", "unknown"),
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Failed to write OKF concept %s: %s", concept_path, exc)
    return False


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Export memory records to an OKF v0.2 bundle.

    Calls ``memory.list`` to retrieve records, then writes each as a
    conformant OKF concept document under ``memories/`` in the bundle.
    """
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    if not bundle_path_str:
        return OperationResult(
            output_text="OKF export memory failed: bundle_path is required.",
        )

    # Reject parent traversal — the storage server re-validates, but
    # we fail early with a clearer error.
    if ".." in bundle_path_str.replace("\\", "/").split("/"):
        return OperationResult(
            output_text="OKF export memory failed: bundle_path may not contain '..' segments.",
        )

    limit = int(ctx.params.get("limit", _DEFAULT_LIMIT) or _DEFAULT_LIMIT)
    limit = max(1, min(limit, _MAX_LIMIT))

    if ctx.host is None:
        return OperationResult(
            output_text="OKF export memory failed: tool execution not injected.",
        )

    # Call memory.list to get all records
    tool_result = await ctx.host.execute_tool(
        "memory.list",
        ToolExecutionContext(
            tool_id="memory.list",
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            user_slug=ctx.user_slug,
            surface_id=ctx.surface_id,
            memory_scope=ctx.memory_scope,
            mode_id=ctx.mode_id,
            parameters={
                "user_slug": ctx.user_slug,
                "limit": limit,
            },
        ),
    )

    if getattr(tool_result, "error", None):
        return OperationResult(
            output_text=f"OKF export memory failed: {tool_result.error}",
        )

    records = _parse_records(tool_result.output_text or "[]")

    if not records:
        return OperationResult(
            output_text="OKF export memory: no records found to export.",
        )

    exported = 0
    skipped = 0

    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        if await _write_record(ctx, bundle_path_str, record):
            exported += 1
        else:
            skipped += 1

    return OperationResult(
        output_text=(
            f"OKF memory export complete.\n"
            f"  Bundle: {bundle_path_str}\n"
            f"  Exported: {exported}\n"
            f"  Skipped: {skipped}\n"
            f"  Total records: {len(records)}"
        ),
    )
