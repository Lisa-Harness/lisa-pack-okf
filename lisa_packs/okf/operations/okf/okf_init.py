"""OKF init operation — scaffold a conformant OKF v0.2 bundle.

No-LLM deterministic write: creates a bundle directory with the two
reserved files (``index.md`` and ``log.md``) as stubs, ready for
concepts to be added by ``okf_emit_concept``.

Every read and write goes through the ``lisa-storage`` MCP server
(``make_dir`` / ``list_files`` / ``write_file``). The pack never touches
the host filesystem directly; ``bundle_path`` is a sandbox-relative
path under ``data_dir/files`` (e.g. ``okf/my-bundle``).
"""

from __future__ import annotations

import logging

from kernel.models import OperationExecutionContext, OperationResult
from pydantic import BaseModel, Field

from lisa_packs.okf.utils.okf_helpers import (
    build_log_entry,
    ensure_bundle_dir,
    format_okf_frontmatter,
    list_bundle_files,
    write_bundle_file,
)

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF init operation."""

    bundle_path: str = Field(
        ...,
        description="Relative path within the lisa-storage files sandbox for the new OKF "
        "bundle directory (e.g. 'okf/my-bundle'). Created if it doesn't exist.",
    )
    title: str = Field(
        default="",
        description="Human-readable name for the bundle (written into index.md).",
    )
    description: str = Field(
        default="",
        description="One-line description of the bundle (written into index.md).",
    )
    force: bool = Field(
        default=False,
        description="If True, allow running against an existing non-empty "
        "directory. Refuses by default.",
    )


def _index_markdown(bundle_name: str, title_str: str, description_str: str) -> str:
    """Build the body of the scaffolded ``index.md``."""
    index_body_lines = [
        f"# {title_str or bundle_name}",
        "",
    ]
    if description_str:
        index_body_lines += [description_str, ""]
    index_body_lines += [
        "This bundle was initialized by `okf_init`.",
        "",
        "Concepts: 0",
        "",
    ]
    return "\n".join(index_body_lines)


async def _write_scaffold_files(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    bundle_name: str,
    title_str: str,
    description_str: str,
) -> str | None:
    """Write the scaffolded ``index.md`` and ``log.md``.

    Returns an error message when a write fails, or ``None`` on success.
    """
    # index.md — directory listing stub
    index_fm = format_okf_frontmatter(
        concept_type="OKF Bundle Index",
        title=title_str or bundle_name,
        description=description_str,
    )
    index_body = _index_markdown(bundle_name, title_str, description_str)
    if not await write_bundle_file(ctx, bundle_path_str, "index.md", index_fm + "\n" + index_body):
        return "OKF init failed: could not write index.md via lisa-storage."

    # log.md — update history, seeded with the scaffold event
    scaffold_entry = build_log_entry(
        action="scaffold",
        by="lisa-pack-okf/0.1.0",
        note=title_str or bundle_name,
    )
    log_body = f"# Bundle Log\n\n{scaffold_entry}\n"
    if not await write_bundle_file(ctx, bundle_path_str, "log.md", log_body):
        return "OKF init failed: could not write log.md via lisa-storage."
    return None


async def _check_existing_bundle(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    *,
    force: bool,
) -> str | None:
    """Return an error message when the bundle may not be scaffolded, else ``None``.

    Detects an existing non-empty bundle via list_files (sandbox-relative).
    """
    try:
        existing = await list_bundle_files(ctx, bundle_path_str)
    except (TypeError, RuntimeError) as exc:
        return f"OKF init failed: {exc}"
    if existing and not force:
        return (
            f"OKF init failed: '{bundle_path_str}' already exists and is not empty "
            f"({len(existing)} file(s)). Pass force=True to scaffold anyway."
        )
    return None


def _validate_bundle_path(bundle_path_str: str) -> str | None:
    """Return an error message for a rejected bundle path, or ``None``.

    Rejects parent traversal — the storage server re-validates, but we
    fail early with a clearer error.
    """
    if not bundle_path_str:
        return "OKF init failed: bundle_path is required."
    if ".." in bundle_path_str.replace("\\", "/").split("/"):
        return "OKF init failed: bundle_path may not contain '..' segments."
    return None


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Scaffold a conformant OKF v0.2 bundle directory under the storage sandbox."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip().strip("/")
    title_str = str(ctx.params.get("title", "")).strip()
    description_str = str(ctx.params.get("description", "")).strip()
    force = bool(ctx.params.get("force", False))

    path_error = _validate_bundle_path(bundle_path_str)
    if path_error is not None:
        return OperationResult(output_text=path_error)

    bundle_name = bundle_path_str.rsplit("/", 1)[-1]

    if ctx.host is None:
        return OperationResult(
            output_text="OKF init failed: tool execution not injected (lisa-storage unavailable).",
        )

    existing_error = await _check_existing_bundle(ctx, bundle_path_str, force=force)
    if existing_error is not None:
        return OperationResult(output_text=existing_error)

    # Create the bundle directory (idempotent).
    if not await ensure_bundle_dir(ctx, bundle_path_str):
        return OperationResult(
            output_text=(
                f"OKF init failed: could not create bundle directory "
                f"'{bundle_path_str}' via lisa-storage."
            ),
        )

    write_error = await _write_scaffold_files(
        ctx,
        bundle_path_str,
        bundle_name,
        title_str,
        description_str,
    )
    if write_error is not None:
        return OperationResult(output_text=write_error)

    return OperationResult(
        output_text=(
            f"OKF bundle initialized: {bundle_path_str}\n"
            f"  index.md: created\n"
            f"  log.md: created (1 entry)\n"
            f"  Use okf_emit_concept to add concepts, then okf_reindex to refresh index.md."
        ),
    )
