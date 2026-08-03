"""OKF log operation — append a structured entry to a bundle's ``log.md``.

No-LLM deterministic write: appends a single timestamped line to the
bundle's update history. This is the low-level primitive that
``okf_init``, ``okf_reindex``, ``okf_emit_concept`` (when it logs),
``okf_verify``, and ``okf_deprecate`` build on. It is also exposed as a
first-class operation so a human or another tool can record events
that happen outside Lisa (e.g. "imported from notion", "reviewed by
team B").

Every read and write goes through the ``lisa-storage`` MCP server
(``read_file`` / ``write_file``). There is no append primitive in
``lisa-storage``; the operation reads the existing ``log.md`` (if any)
and rewrites the whole file with the new entry appended. ``bundle_path``
is a sandbox-relative path under ``data_dir/files`` (e.g.
``okf/my-bundle``).
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult
from lisa_packs.okf.utils.okf_helpers import append_log_entry_storage, build_log_entry

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF log operation."""

    bundle_path: str = Field(
        ...,
        description="Relative path within the lisa-storage files sandbox for the OKF bundle whose log.md should be appended to.",
    )
    action: str = Field(
        ...,
        description="Short verb describing the event (e.g. 'reviewed', 'imported', 'exported').",
    )
    concept_path: str = Field(
        default="",
        description="Relative path of the affected concept, if any.",
    )
    by: str = Field(
        default="",
        description="Actor string for the log entry (e.g. 'human:adam'). Defaults to lisa-pack-okf/0.1.0.",
    )
    note: str = Field(
        default="",
        description="Free-form one-line note explaining the event.",
    )


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Append a structured entry to an OKF bundle's ``log.md`` via lisa-storage."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip().strip("/")
    if not bundle_path_str:
        return OperationResult(output_text="OKF log failed: bundle_path is required.")

    action_str = str(ctx.params.get("action", "")).strip()
    if not action_str:
        return OperationResult(output_text="OKF log failed: action is required.")

    concept_path_str = str(ctx.params.get("concept_path", "")).strip()
    by_str = str(ctx.params.get("by", "")).strip() or "lisa-pack-okf/0.1.0"
    note_str = str(ctx.params.get("note", "")).strip()

    if ".." in bundle_path_str.replace("\\", "/").split("/"):
        return OperationResult(
            output_text="OKF log failed: bundle_path may not contain '..' segments.",
        )

    if ctx.host is None:
        return OperationResult(
            output_text="OKF log failed: tool execution not injected (lisa-storage unavailable).",
        )

    entry = build_log_entry(
        action=action_str,
        concept_path=concept_path_str,
        by=by_str,
        note=note_str,
    )
    ok = await append_log_entry_storage(ctx, bundle_path_str, entry)
    if not ok:
        return OperationResult(
            output_text="OKF log failed: could not write log.md via lisa-storage.",
        )

    return OperationResult(
        output_text=(
            f"OKF log entry appended: {bundle_path_str}/log.md\n"
            f"  {entry}"
        ),
    )