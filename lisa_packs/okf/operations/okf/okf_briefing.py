"""OKF briefing operation — deterministic pre-turn context skim.

Surfaces relevant OKF concepts from a working bundle before a mode's
main turn, without polluting the model's context with the full bundle.
The host runs this via ``pre_turn_operation_ids`` (mirroring the
``search`` pack's ``discussion_briefing`` seam); the returned
``output_text`` is injected as a bounded advisory block the model
treats as "inform your view", not as instruction.

Deterministic, no-LLM: walks the working bundle via ``lisa-storage``
(``list_files`` + ``read_file``), parses frontmatter, filters to
non-stale ``stable`` concepts whose ``type``/``title``/``description``/
``tags`` overlap the user's query (and optionally the active mode's
intent tags), and returns a compact cited bullet list. Each bullet
names the concept's in-bundle path so the model can call ``read_file``
for the full text if a bullet is relevant — a skim, not a dump.

Returns an empty ``output_text`` (host renders nothing) when the
query is short/acknowledgement, the bundle is empty, or no concept
clears the relevance floor. That is the no-pollution contract.
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

from kernel.models import OperationExecutionContext, OperationResult
from pydantic import BaseModel, Field

from lisa_packs.okf.utils.okf_helpers import (
    RESERVED_FILENAMES,
    filter_briefing_concepts,
    list_bundle_files,
    parse_frontmatter,
    read_bundle_file,
    render_briefing,
)

logger = logging.getLogger(__name__)

__all__ = ["OkfBriefingStructuredOutput", "Parameters", "execute"]

# Default working bundle path. Modes that want a pre-turn OKF skim
# without configuring a path brief from here; ``okf_init okf/working``
# scaffolds it. This is the OKF working-memory convention: the bundle
# at ``okf/working`` is "the shared scratchpad", the way ``memory.search``
# is "the long-term store". No mode needs to be told where it is.
DEFAULT_BUNDLE_PATH = "okf/working"

# Acknowledgement / filler queries that should not trigger a briefing.
# Mirrors the decision rule in ``discussion_briefing.operation.md``.
_ACK_PATTERNS = re.compile(
    r"^(y(es|eah|ep)?|ok(ay)?|sure|nope?|thanks|thx|ty|cool|got ?it|makes sense|"
    r"noted|agree|done|k|pls|please)[.!?]*$",
    re.IGNORECASE,
)

# Hard caps that bound the briefing even when many concepts are
# relevant. The pre-turn block must stay small or it becomes the
# pollution it was designed to prevent.
_MAX_BULLETS = 5
_MAX_BRIEFING_BYTES = 1500

# Longest query that can still be a pure acknowledgement. Anything
# longer is treated as a real question and always gets a briefing.
_MAX_ACK_QUERY_CHARS = 40


class Parameters(BaseModel):
    """User-supplied inputs for the OKF briefing operation."""

    bundle_path: str = Field(
        default=DEFAULT_BUNDLE_PATH,
        description=(
            "Sandbox-relative path to the OKF bundle to brief from "
            f"(default '{DEFAULT_BUNDLE_PATH}')."
        ),
    )
    query: str = Field(
        default="",
        description="The user's turn query, used for relevance scoring.",
    )
    mode_intent_tags: list[str] = Field(
        default_factory=list,
        description="Active mode's intent tags, used to boost concept overlap.",
    )
    max_bullets: int = Field(
        default=_MAX_BULLETS,
        description="Maximum number of concepts to surface.",
        ge=0,
        le=20,
    )
    max_bytes: int = Field(
        default=_MAX_BRIEFING_BYTES,
        description="Byte ceiling on the rendered briefing block.",
        ge=0,
    )


class OkfBriefingStructuredOutput(BaseModel):
    """Structured envelope for declarative pre-turn wiring.

    Runtime (deterministic) callers read ``output_text`` from the
    ``OperationResult`` directly; this model exists so a host that
    prefers the declarative ``response_format_model_path`` seam can
    consume the same operation without a parallel implementation.
    """

    output_text: str = Field(
        default="",
        description="Briefing markdown, or empty when no context is surfaced.",
    )
    surfaced_concepts: list[str] = Field(
        default_factory=list,
        description="In-bundle paths of the concepts surfaced, in order.",
    )
    bundle_path: str = Field(
        default="",
        description="The bundle the briefing was read from.",
    )


def _str_value(data: dict[str, object], key: str) -> str:
    val = data.get(key)
    if isinstance(val, str):
        return val
    # YAML safe_load parses unquoted YYYY-MM-DD as a datetime.date;
    # stringify it so frontmatter dates survive as 'YYYY-MM-DD'.
    if val is not None:
        return str(val)
    return ""


def _tags(data: dict[str, object]) -> list[str]:
    tags_val = data.get("tags")
    if isinstance(tags_val, list):
        return [str(t) for t in tags_val if t is not None]
    return []


def _is_acknowledgement(query: str) -> bool:
    """Return True for short acknowledgement/filler queries.

    These should not trigger a briefing — surfacing context here is
    pollution, not signal.
    """
    q = query.strip().lower()
    if not q or len(q) > _MAX_ACK_QUERY_CHARS:
        # Long queries are never pure acknowledgements; short non-empty
        # ones are checked against the pattern.
        return len(q) == 0
    return bool(_ACK_PATTERNS.match(q))


def _empty_briefing(bundle_path_str: str) -> OperationResult:
    """Return the no-pollution empty briefing result for a bundle path."""
    return OperationResult(
        output_text="",
        output_json={
            "output_text": "",
            "surfaced_concepts": [],
            "bundle_path": bundle_path_str,
        },
    )


class _BriefingParams(NamedTuple):
    """The briefing operation's resolved inputs."""

    bundle_path: str
    query: str
    mode_intent_tags: list[str]
    max_bullets: int
    max_bytes: int
    today: str | None


def _briefing_params(ctx: OperationExecutionContext) -> _BriefingParams:
    """Extract the briefing inputs from the operation context params."""
    bundle_path_str = str(ctx.params.get("bundle_path", DEFAULT_BUNDLE_PATH)).strip().strip("/")
    query_str = str(ctx.params.get("query", "")).strip()
    mode_intent_tags_raw = ctx.params.get("mode_intent_tags", [])
    mode_intent_tags: list[str] = []
    if isinstance(mode_intent_tags_raw, list):
        mode_intent_tags = [str(t) for t in mode_intent_tags_raw if t]
    max_bullets = int(ctx.params.get("max_bullets", _MAX_BULLETS) or _MAX_BULLETS)
    max_bytes = int(ctx.params.get("max_bytes", _MAX_BRIEFING_BYTES) or _MAX_BRIEFING_BYTES)
    today = str(ctx.params.get("today", "")).strip() or None
    return _BriefingParams(
        bundle_path=bundle_path_str,
        query=query_str,
        mode_intent_tags=mode_intent_tags,
        max_bullets=max_bullets,
        max_bytes=max_bytes,
        today=today,
    )


def _in_bundle_rel_path(sandbox_path: str, bundle_path_str: str) -> str:
    """Return the in-bundle relative path for a sandbox-relative path."""
    p = sandbox_path.replace("\\", "/")
    prefix = bundle_path_str.replace("\\", "/").strip("/")
    return p[len(prefix) + 1 :] if prefix and p.startswith(prefix + "/") else p


async def _collect_concepts(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    files: list[str],
) -> list[dict[str, object]]:
    """Read every concept document in a bundle and return their metadata."""
    concepts: list[dict[str, object]] = []
    for sandbox_path in files:
        rel_path = _in_bundle_rel_path(sandbox_path, bundle_path_str)
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
            {
                "path": rel_path,
                "type": type_val,
                "title": _str_value(frontmatter, "title"),
                "description": _str_value(frontmatter, "description"),
                "tags": _tags(frontmatter),
                "status": _str_value(frontmatter, "status") or "stable",
                "stale_after": _str_value(frontmatter, "stale_after"),
            }
        )
    return concepts


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Skim the working OKF bundle for concepts relevant to the user's turn."""
    params = _briefing_params(ctx)
    bundle_path_str = params.bundle_path
    query_str = params.query
    mode_intent_tags = params.mode_intent_tags
    max_bullets = params.max_bullets
    max_bytes = params.max_bytes
    today = params.today

    # No-pollution guard: acknowledgement/filler → empty briefing.
    if _is_acknowledgement(query_str):
        return _empty_briefing(bundle_path_str)

    if ".." in bundle_path_str.replace("\\", "/").split("/"):
        return OperationResult(
            output_text="OKF briefing failed: bundle_path may not contain '..' segments.",
        )

    if ctx.host is None:
        # No storage available (e.g. bare host) → empty briefing, not a
        # hard failure. A missing briefing is a no-op for the turn; a
        # hard failure would break the host's pre-turn pipeline.
        logger.debug("okf_briefing: execute_tool_fn not injected; returning empty briefing")
        return _empty_briefing(bundle_path_str)

    try:
        files = await list_bundle_files(ctx, bundle_path_str)
    except (TypeError, RuntimeError) as exc:
        # Storage not wired or unavailable — empty briefing, not a hard failure.
        logger.debug("okf_briefing: storage unavailable (%s); returning empty briefing", exc)
        return _empty_briefing(bundle_path_str)

    concepts = await _collect_concepts(ctx, bundle_path_str, files)

    ranked = filter_briefing_concepts(
        concepts,
        query=query_str,
        mode_intent_tags=mode_intent_tags or None,
        today=today,
    )

    briefing = render_briefing(
        ranked,
        bundle_path=bundle_path_str,
        max_bullets=max_bullets,
        max_bytes=max_bytes,
    )

    surfaced = [str(c.get("path", "")) for c in ranked[:max_bullets]]

    return OperationResult(
        output_text=briefing,
        output_json={
            "output_text": briefing,
            "surfaced_concepts": surfaced,
            "bundle_path": bundle_path_str,
        },
    )
