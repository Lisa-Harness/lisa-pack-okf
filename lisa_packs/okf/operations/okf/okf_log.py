"""OKF log operation — append a structured entry to a bundle's ``log.md``.

No-LLM deterministic write: appends a single timestamped line to the
bundle's update history. This is the low-level primitive that
``okf_init``, ``okf_reindex``, ``okf_emit_concept`` (when it logs),
``okf_verify``, and ``okf_deprecate`` build on. It is also exposed as a
first-class operation so a human or another tool can record events
that happen outside Lisa (e.g. "imported from notion", "reviewed by
team B").
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult
from lisa_packs.okf.utils.okf_helpers import append_log_entry, build_log_entry

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF log operation."""

    bundle_path: str = Field(
        ...,
        description="Absolute path to the OKF bundle directory whose log.md should be appended to.",
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
    """Append a structured entry to an OKF bundle's ``log.md``."""
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    if not bundle_path_str:
        return OperationResult(output_text="OKF log failed: bundle_path is required.")

    action_str = str(ctx.params.get("action", "")).strip()
    if not action_str:
        return OperationResult(output_text="OKF log failed: action is required.")

    concept_path_str = str(ctx.params.get("concept_path", "")).strip()
    by_str = str(ctx.params.get("by", "")).strip() or "lisa-pack-okf/0.1.0"
    note_str = str(ctx.params.get("note", "")).strip()

    bundle = Path(bundle_path_str)
    if not bundle.is_dir():
        return OperationResult(
            output_text=f"OKF log failed: path is not a directory: {bundle_path_str}",
        )

    entry = build_log_entry(
        action=action_str,
        concept_path=concept_path_str,
        by=by_str,
        note=note_str,
    )
    log_path = append_log_entry(bundle, entry)

    return OperationResult(
        output_text=(
            f"OKF log entry appended: {log_path.relative_to(bundle)}\n"
            f"  {entry}"
        ),
    )