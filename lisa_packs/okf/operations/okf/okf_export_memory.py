"""OKF export memory operation — export memory records to an OKF v0.2 bundle.

No-LLM deterministic export: calls ``memory.list`` to retrieve records,
then writes each as an OKF concept document. Uses the shared OKF helpers
for frontmatter formatting and file writing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult, ToolExecutionContext
from lisa_packs.okf.utils.okf_helpers import (
    format_okf_frontmatter,
    sanitize_filename,
    write_concept_file,
)

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF export memory operation."""

    bundle_path: str = Field(
        ...,
        description="Absolute path to the OKF bundle directory to write into. Created if it doesn't exist.",
    )
    limit: int = Field(
        default=100,
        description="Maximum number of memory records to export.",
        ge=1,
        le=1000,
    )


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

    limit = int(ctx.params.get("limit", 100) or 100)
    limit = max(1, min(limit, 1000))

    bundle = Path(bundle_path_str)
    bundle.mkdir(parents=True, exist_ok=True)

    if not callable(ctx.execute_tool_fn):
        return OperationResult(
            output_text="OKF export memory failed: tool execution not injected.",
        )

    # Call memory.list to get all records
    tool_result = await ctx.execute_tool_fn(
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

    # Parse the JSON result
    records: list[dict] = []
    try:
        data = json.loads(tool_result.output_text or "[]")
    except (json.JSONDecodeError, TypeError):
        data = []

    if isinstance(data, dict) and "results" in data:
        results = data.get("results")
        if isinstance(results, list):
            data = results

    if isinstance(data, list):
        records = data

    if not records:
        return OperationResult(
            output_text=f"OKF export memory: no records found to export.",
        )

    exported = 0
    skipped = 0

    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue

        content = str(record.get("content", "")).strip()
        if not content:
            skipped += 1
            continue

        # Derive title from first ~60 chars of content
        title = content[:60].replace("\n", " ").strip()
        if len(content) > 60:
            title += "…"

        # Derive tags from memory metadata
        tags: list[str] = []
        tier = str(record.get("memory_tier", ""))
        if tier and tier != "daily":
            tags.append(tier)
        op_id = str(record.get("operation_id", ""))
        if op_id and op_id not in ("mcp.add_memory", "mcp.v1"):
            tags.append(op_id.replace("_", "-"))

        # Build a safe filename from the record id or title
        record_id = str(record.get("id", ""))
        if record_id:
            filename = f"{sanitize_filename(record_id)}.md"
        else:
            filename = f"{sanitize_filename(title[:40])}.md"

        concept_path = f"memories/{filename}"

        # Build the body with metadata
        body_lines = [f"# {title}", ""]
        body_lines.append(content)
        body_lines.append("")

        # Add metadata as a table
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

        body = "\n".join(body_lines)

        frontmatter = format_okf_frontmatter(
            concept_type="Memory Record",
            title=title,
            description=content[:120].replace("\n", " ").strip(),
            tags=tags if tags else None,
        )

        try:
            write_concept_file(bundle, concept_path, frontmatter, body)
            exported += 1
        except OSError as exc:
            logger.warning("Failed to write OKF concept %s: %s", concept_path, exc)
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
