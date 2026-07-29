"""OKF init operation — scaffold a conformant OKF v0.2 bundle.

No-LLM deterministic write: creates a bundle directory with the two
reserved files (``index.md`` and ``log.md``) as stubs, ready for
concepts to be added by ``okf_emit_concept``.

Operates on the local filesystem (``pathlib``) — the same model as
``okf_validate`` and ``okf_ingest``. This is the first-run experience
for a new bundle; it does not go through ``lisa-storage`` because there
is nothing to sandbox yet.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult
from lisa_packs.okf.utils.okf_helpers import (
    append_log_entry,
    build_log_entry,
    format_okf_frontmatter,
)

logger = logging.getLogger(__name__)


class Parameters(BaseModel):
    """User-supplied inputs for the OKF init operation."""

    bundle_path: str = Field(
        ...,
        description="Absolute path for the new OKF bundle directory. Created if it doesn't exist.",
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
        description="If True, allow running against an existing non-empty directory. Refuses by default.",
    )


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Scaffold a conformant OKF v0.2 bundle directory."""
    from pathlib import Path

    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    if not bundle_path_str:
        return OperationResult(output_text="OKF init failed: bundle_path is required.")

    title_str = str(ctx.params.get("title", "")).strip()
    description_str = str(ctx.params.get("description", "")).strip()
    force = bool(ctx.params.get("force", False))

    bundle = Path(bundle_path_str)

    if bundle.exists() and any(bundle.iterdir()) and not force:
        return OperationResult(
            output_text=(
                f"OKF init failed: '{bundle_path_str}' already exists and is not empty. "
                f"Pass force=True to scaffold anyway."
            ),
        )

    bundle.mkdir(parents=True, exist_ok=True)

    # index.md — directory listing stub
    index_fm = format_okf_frontmatter(
        concept_type="OKF Bundle Index",
        title=title_str or bundle.name,
        description=description_str,
    )
    index_body_lines = [
        f"# {title_str or bundle.name}",
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
    index_body = "\n".join(index_body_lines)
    (bundle / "index.md").write_text(index_fm + "\n" + index_body, encoding="utf-8")

    # log.md — update history, seeded with the scaffold event
    scaffold_entry = build_log_entry(
        action="scaffold",
        by="lisa-pack-okf/0.1.0",
        note=title_str or bundle.name,
    )
    append_log_entry(bundle, scaffold_entry)

    return OperationResult(
        output_text=(
            f"OKF bundle initialized: {bundle_path_str}\n"
            f"  index.md: created\n"
            f"  log.md: created (1 entry)\n"
            f"  Use okf_emit_concept to add concepts, then okf_reindex to refresh index.md."
        ),
    )