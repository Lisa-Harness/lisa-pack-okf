"""OKF emit concept operation — write a single OKF v0.2 concept document.

No-LLM deterministic write: takes concept metadata and body text, produces
a conformant OKF markdown file with YAML frontmatter. Writes through the
``lisa-storage`` MCP server's ``write_file`` tool — never touches the
filesystem directly.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult, ToolExecutionContext
from lisa_packs.okf.utils.okf_helpers import (
    RESERVED_FILENAMES,
    extract_resource,
    format_okf_frontmatter,
    is_duplicate_concept_path,
    parse_frontmatter,
)

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF emit concept operation."""

    bundle_path: str = Field(
        ...,
        description="Relative path within the lisa-storage files sandbox for the OKF bundle directory (e.g. 'okf/my-bundle').",
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
    generated_by: str = Field(
        default="",
        description="Actor who produced the content (e.g. 'human:adam'). Defaults to lisa-pack-okf/0.1.0.",
    )
    resource: str = Field(
        default="",
        description="Canonical URI for the underlying asset, written into frontmatter.",
    )
    stale_after: str = Field(
        default="",
        description="YYYY-MM-DD date after which the concept is considered stale.",
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
    generated_by_str = str(ctx.params.get("generated_by", "")).strip() or "lisa-pack-okf/0.1.0"
    resource_str = str(ctx.params.get("resource", "")).strip()
    stale_after_str = str(ctx.params.get("stale_after", "")).strip()

    if not bundle_path_str:
        return OperationResult(output_text="OKF emit failed: bundle_path is required.")
    if not concept_path_str:
        return OperationResult(output_text="OKF emit failed: concept_path is required.")
    if not type_str:
        return OperationResult(output_text="OKF emit failed: type is required (OKF v0.2 §4.1).")

    # Validate concept_path doesn't use reserved filenames
    filename = concept_path_str.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if filename in ("index.md", "log.md"):
        return OperationResult(
            output_text=f"OKF emit failed: '{filename}' is a reserved filename (OKF v0.2 §3.1).",
        )

    # Reject absolute paths and parent traversal — the storage server
    # re-validates, but we fail early with a clearer error.
    for part in (bundle_path_str + "/" + concept_path_str).replace("\\", "/").split("/"):
        if part == "..":
            return OperationResult(
                output_text="OKF emit failed: paths may not contain '..' segments.",
            )

    if ctx.host is None:
        return OperationResult(
            output_text="OKF emit failed: tool execution not injected (lisa-storage write_file unavailable).",
        )

    # Combine bundle_path + concept_path into a single sandbox-relative path.
    full_path = f"{bundle_path_str}/{concept_path_str}"
    if not full_path.endswith(".md"):
        full_path += ".md"

    # Resource-as-identity dedup (2026-07-28): refuse to emit a concept
    # whose canonical ``resource`` URI already exists at a DIFFERENT path
    # in the bundle. Same-path re-emit is an idempotent update and stays
    # allowed. Skipped when ``resource`` is empty (no identity key).
    if resource_str:
        existing = await _find_duplicate_resource(
            ctx=ctx,
            bundle_path_str=bundle_path_str,
            resource_str=resource_str,
            concept_full_path=full_path,
        )
        if existing is not None:
            return OperationResult(
                output_text=(
                    f"OKF emit failed: a concept with resource '{resource_str}' "
                    f"already exists at '{existing}'. Re-emit at the same path "
                    f"to update in place, or delete the stale copy first. "
                    f"(Refusing to create a duplicate — resource is the OKF "
                    f"concept identity key.)"
                ),
            )

    frontmatter = format_okf_frontmatter(
        concept_type=type_str,
        title=title_str,
        description=description_str,
        tags=tags if tags else None,
        status=status_str,
        generated_by=generated_by_str,
        resource=resource_str,
        stale_after=stale_after_str,
    )

    content = frontmatter
    if body_str:
        content += f"\n{body_str}\n"

    tool_result = await ctx.host.execute_tool(
        "write_file",
        ToolExecutionContext(
            tool_id="write_file",
            workspace_id=ctx.workspace_id,
            user_id=ctx.user_id,
            user_slug=ctx.user_slug,
            surface_id=ctx.surface_id,
            memory_scope=ctx.memory_scope,
            mode_id=ctx.mode_id,
            parameters={"path": full_path, "content": content},
        ),
    )

    result_data = json.loads(tool_result.output_text or "{}")
    if not result_data.get("ok"):
        return OperationResult(
            output_text=f"OKF emit failed: storage write_file error: {result_data.get('error', 'unknown')}",
        )

    return OperationResult(
        output_text=(
            f"OKF concept emitted: {full_path}\n"
            f"  type: {type_str}\n"
            f"  title: {title_str or '(untitled)'}\n"
            f"  bundle: {bundle_path_str}"
        ),
    )


async def _find_duplicate_resource(
    *,
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    resource_str: str,
    concept_full_path: str,
) -> str | None:
    """Return the existing path of a concept with the same ``resource``.

    Scans the bundle via the ``lisa-storage`` ``list_files`` and ``read_file``
    tools (never touches the filesystem directly). Returns the relative
    bundle path of the first existing concept whose ``resource`` matches
    ``resource_str`` AND whose path differs from ``concept_full_path``
    (same-path re-emit is an idempotent update, not a duplicate). Returns
    ``None`` when no duplicate is found, when the bundle is empty, or when
    a storage tool call fails (dedup degrades to a no-op rather than
    blocking a legitimate emit on a transient storage error — the write
    itself still goes through ``write_file`` which re-validates paths).
    """
    try:
        list_result = await ctx.host.execute_tool(
            "list_files",
            ToolExecutionContext(
                tool_id="list_files",
                workspace_id=ctx.workspace_id,
                user_id=ctx.user_id,
                user_slug=ctx.user_slug,
                surface_id=ctx.surface_id,
                memory_scope=ctx.memory_scope,
                mode_id=ctx.mode_id,
                parameters={"prefix": bundle_path_str},
            ),
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("OKF dedup: list_files failed (%s); skipping dedup.", exc)
        return None

    try:
        files = json.loads(list_result.output_text or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(files, list):
        return None

    target_resource = resource_str.strip()
    if not target_resource:
        return None

    for rel_path in files:
        if not isinstance(rel_path, str) or not rel_path.endswith(".md"):
            continue
        # Skip reserved filenames (index.md / log.md live at the bundle root
        # and are not concepts).
        if rel_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] in RESERVED_FILENAMES:
            continue
        # Same-path re-emit is an update, not a duplicate.
        if is_duplicate_concept_path(rel_path, concept_full_path):
            continue
        try:
            read_result = await ctx.host.execute_tool(
                "read_file",
                ToolExecutionContext(
                    tool_id="read_file",
                    workspace_id=ctx.workspace_id,
                    user_id=ctx.user_id,
                    user_slug=ctx.user_slug,
                    surface_id=ctx.surface_id,
                    memory_scope=ctx.memory_scope,
                    mode_id=ctx.mode_id,
                    parameters={"path": rel_path},
                ),
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("OKF dedup: read_file failed for %s (%s).", rel_path, exc)
            continue
        text = read_result.output_text or ""
        # A read_file error comes back as a JSON body, not markdown —
        # parse_frontmatter returns None for non-markdown text, so this
        # is safely skipped.
        frontmatter = parse_frontmatter(text)
        if frontmatter is None:
            continue
        if extract_resource(frontmatter) == target_resource:
            return rel_path

    return None
