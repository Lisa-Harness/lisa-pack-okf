"""Shared OKF helpers for the OKF pack.

Pure functions for frontmatter, log-entry, and index formatting, plus
storage-backed I/O helpers that route every bundle read and write
through the ``lisa-storage`` MCP server (``write_file`` / ``read_file`` /
``list_files`` / ``make_dir``). The OKF pack never touches the host
filesystem directly — every bundle lives under the storage sandbox
(``data_dir/files``), addressed by sandbox-relative paths.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from kernel.models import OperationExecutionContext, ToolExecutionContext

logger = logging.getLogger(__name__)


def format_okf_frontmatter(
    *,
    concept_type: str,
    title: str = "",
    description: str = "",
    tags: list[str] | None = None,
    status: str = "stable",
    generated_by: str = "lisa-pack-okf/0.1.0",
    generated_at: str = "",
    resource: str = "",
    stale_after: str = "",
    sources: list[dict[str, object]] | None = None,
    verified: list[dict[str, object]] | None = None,
) -> str:
    """Build the YAML frontmatter block for an OKF concept document.

    Args:
        concept_type: The concept type (required OKF field).
        title: Human-readable display name.
        description: One-line summary.
        tags: Categorization tags.
        status: Lifecycle status (draft, stable, deprecated).
        generated_by: Actor string for ``generated.by``.
        generated_at: Override for ``generated.at`` (ISO 8601). Defaults to now.
        resource: Canonical URI for the underlying asset.
        stale_after: YYYY-MM-DD date after which the concept is stale.
        sources: List of source mappings (``{id, resource, title, ...}``).
        verified: List of verification mappings (``{by, at}``).
    """
    now = generated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    fm: dict[str, object] = {
        "type": concept_type,
        "generated": {"by": generated_by, "at": now},
    }

    if title:
        fm["title"] = title
    if description:
        fm["description"] = description
    if tags:
        fm["tags"] = tags
    if status and status != "stable":
        fm["status"] = status
    if resource:
        fm["resource"] = resource
    if stale_after:
        fm["stale_after"] = stale_after
    if sources:
        fm["sources"] = sources
    if verified:
        fm["verified"] = verified

    yaml_text = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{yaml_text}---\n"


def sanitize_filename(name: str) -> str:
    """Convert a string into a safe filename component.

    Replaces spaces and special characters with hyphens, collapses
    runs, and strips leading/trailing separators.
    """
    import re

    safe = re.sub(r"[^a-z0-9]+", "-", name.lower())
    safe = safe.strip("-")
    return safe or "untitled"


# ---------------------------------------------------------------------------
# Bundle-level helpers (index.md + log.md — OKF v0.2 §3.1)
# ---------------------------------------------------------------------------


def build_index_markdown(concepts: list[dict[str, object]]) -> str:
    """Build the body of an OKF ``index.md`` directory listing.

    Args:
        concepts: list of dicts with at least ``path`` and ``type`` keys;
            ``title`` and ``description`` are included when present.

    Returns:
        Markdown body (without frontmatter) for ``index.md``.
    """
    lines: list[str] = [
        "# Bundle Index",
        "",
        f"{len(concepts)} concept{'s' if len(concepts) != 1 else ''}.",
        "",
        "| Path | Type | Title | Description |",
        "| --- | --- | --- | --- |",
    ]
    for concept in sorted(concepts, key=lambda c: str(c.get("path", ""))):
        path = str(concept.get("path", ""))
        ctype = str(concept.get("type", ""))
        title = str(concept.get("title", "") or "")
        desc = str(concept.get("description", "") or "")
        lines.append(f"| `{path}` | {ctype} | {title} | {desc} |")
    lines.append("")
    return "\n".join(lines)


def build_log_entry(
    *,
    action: str,
    concept_path: str = "",
    by: str = "",
    note: str = "",
) -> str:
    """Format a single append-only line for OKF ``log.md``.

    Args:
        action: short verb (``emit``, ``verify``, ``deprecate``, ``scaffold``...).
        concept_path: relative path of the affected concept, or empty for bundle-level.
        by: actor string (``human:adam``, ``lisa-pack-okf/0.1.0``...).
        note: free-form one-line note.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"- {now} {action}"]
    if concept_path:
        parts.append(f"`{concept_path}`")
    if by:
        parts.append(f"by {by}")
    if note:
        parts.append(f"— {note}")
    return " ".join(parts)


def append_log_entry(bundle: Path, entry: str) -> Path:
    """[Deprecated filesystem variant — kept only for legacy callers.]

    New OKF code routes log appends through :func:`append_log_entry_storage`
    so ``log.md`` lives under the ``lisa-storage`` sandbox like every
    other bundle file. This synchronous ``pathlib`` version is retained
    for any out-of-band tooling that operates on a checked-out bundle on
    disk; the pack operations themselves do not call it.
    """
    log_path = bundle / "log.md"
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")
        # Keep a trailing newline so append lands on its own line
        if not existing.endswith("\n"):
            existing += "\n"
        log_path.write_text(existing + entry + "\n", encoding="utf-8")
    else:
        log_path.write_text(
            f"# Bundle Log\n\n{entry}\n",
            encoding="utf-8",
        )
    return log_path


# ---------------------------------------------------------------------------
# Storage-backed I/O helpers (lisa-storage MCP server)
# ---------------------------------------------------------------------------


def _tool_ctx_from(
    ctx: OperationExecutionContext,
    tool_id: str,
    parameters: dict[str, Any],
) -> ToolExecutionContext:
    """Build a ToolExecutionContext mirroring the caller's operation context."""
    from kernel.models import ToolExecutionContext

    return ToolExecutionContext(
        tool_id=tool_id,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        user_slug=ctx.user_slug,
        surface_id=ctx.surface_id,
        memory_scope=ctx.memory_scope,
        mode_id=ctx.mode_id,
        parameters=parameters,
    )


async def storage_call(
    ctx: OperationExecutionContext,
    tool_id: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Invoke a ``lisa-storage`` tool and return its parsed JSON result.

    Storage tools always return a JSON string (either ``{"ok": ...}`` or
    ``{"error": ...}``). This helper centralizes the execute → parse path
    so every operation handles storage failures the same way. Raises
    ``TypeError`` if the tool executor is not injected or the call
    itself throws — callers wrap broad-except around it when they want
    degradation rather than a hard failure.
    """
    if not callable(ctx.execute_tool_fn):
        raise TypeError("tool execution not injected (lisa-storage unavailable)")
    result = await ctx.execute_tool_fn(tool_id, _tool_ctx_from(ctx, tool_id, parameters))
    text = getattr(result, "output_text", "") or ""
    try:
        parsed = json.loads(text) if text else {}
    except (json.JSONDecodeError, TypeError):
        # ``read_file`` returns raw file content (not JSON) on success;
        # surface it under a ``content`` key so callers can tell.
        return {"ok": True, "content": text}
    return parsed if isinstance(parsed, dict) else {"ok": True, "raw": parsed}


def _bundle_rel(bundle_path_str: str, *parts: str) -> str:
    """Join a sandbox-relative bundle path with sub-path parts.

    Normalizes backslashes to forward slashes and strips duplicate
    slashes so the result is a single clean sandbox-relative path
    (e.g. ``okf/my-bundle/log.md``).
    """
    segments = [bundle_path_str.strip().strip("/"), *(p.strip().strip("/") for p in parts)]
    joined = "/".join(s for s in segments if s)
    return joined.replace("\\", "/")


async def read_bundle_file(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    rel_path: str,
) -> str | None:
    """Read a file from a bundle via ``lisa-storage`` ``read_file``.

    Returns the file content as a string, or ``None`` if the file does
    not exist or the read fails. A storage error JSON body is mapped to
    ``None`` (callers treat missing files as "not yet created"); a
    thrown exception propagates to the caller's broad-except handler.
    """
    full = _bundle_rel(bundle_path_str, rel_path)
    data = await storage_call(ctx, "read_file", {"path": full})
    if "error" in data:
        return None
    # ``read_file`` success returns raw text → storage_call surfaces it
    # under ``content``. A non-error JSON dict without ``content`` would
    # be an unexpected shape; treat it as empty.
    return str(data.get("content", "")) if "content" in data else ""


async def list_bundle_files(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
) -> list[str]:
    """List all files in a bundle via ``lisa-storage`` ``list_files``.

    Returns sandbox-relative paths (e.g. ``okf/my-bundle/modes/learn.md``)
    sorted, or an empty list if the bundle does not exist or listing
    fails. The storage server filters reserved/denied paths itself.
    """
    data = await storage_call(ctx, "list_files", {"prefix": bundle_path_str})
    if "error" in data:
        return []
    files = data.get("raw", data.get("content", []))
    if isinstance(files, list):
        return [str(f) for f in files if isinstance(f, str)]
    return []


async def write_bundle_file(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    rel_path: str,
    content: str,
) -> bool:
    """Write a file into a bundle via ``lisa-storage`` ``write_file``.

    Parent directories are created by the storage server. Returns
    ``True`` on success, ``False`` on a storage error.
    """
    full = _bundle_rel(bundle_path_str, rel_path)
    data = await storage_call(ctx, "write_file", {"path": full, "content": content})
    return bool(data.get("ok"))


async def ensure_bundle_dir(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
) -> bool:
    """Ensure the bundle directory exists under the storage sandbox.

    ``make_dir`` is idempotent (``exist_ok=True``). Returns ``True`` on
    success, ``False`` on a storage error.
    """
    data = await storage_call(ctx, "make_dir", {"path": bundle_path_str.strip().strip("/")})
    return bool(data.get("ok"))


async def append_log_entry_storage(
    ctx: OperationExecutionContext,
    bundle_path_str: str,
    entry: str,
) -> bool:
    """Append a line to the bundle's ``log.md`` through ``lisa-storage``.

    Reads the existing ``log.md`` (if any), appends the entry on its own
    line, and writes the whole file back via ``write_file``. There is no
    append primitive in ``lisa-storage``; read-rewrite is the contract.
    Creates ``log.md`` with a header if it does not yet exist.

    Returns ``True`` on success, ``False`` if the write fails.
    """
    existing = await read_bundle_file(ctx, bundle_path_str, "log.md")
    if existing is None:
        body = f"# Bundle Log\n\n{entry}\n"
    else:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        body = existing + entry + "\n"
    return await write_bundle_file(ctx, bundle_path_str, "log.md", body)


# ---------------------------------------------------------------------------
# Duplicate detection (resource-as-identity)
# ---------------------------------------------------------------------------

# Reserved filenames excluded from duplicate scans (OKF v0.2 §3.1).
# Duplicated here from the ops so the helper is self-contained.
RESERVED_FILENAMES: frozenset[str] = frozenset({"index.md", "log.md"})


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """Parse YAML frontmatter from a markdown file's text.

    Returns the parsed dict, or ``None`` if no parseable frontmatter block
    is found. Shared by emit/validate dedup scans so every caller parses
    frontmatter the same way.
    """
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


def extract_resource(frontmatter: dict[str, object]) -> str:
    """Return the canonical ``resource`` URI from frontmatter, or ''.

    The ``resource`` field is the OKF concept identity key (one concept
    == one ``resource`` per bundle). An empty value means the concept
    has no identity key and dedup is skipped for it.
    """
    val = frontmatter.get("resource")
    if isinstance(val, str):
        return val.strip()
    return ""


def is_duplicate_concept_path(candidate: str, existing: str) -> bool:
    """Return True if ``candidate`` and ``existing`` reference the same file.

    Path comparison is case-insensitive and slash-agnostic (``\\`` vs ``/``)
    so a Windows caller and a Unix-written bundle agree on identity.
    """
    def _norm(p: str) -> str:
        return p.strip().replace("\\", "/").lstrip("/").lower()

    return _norm(candidate) == _norm(existing)


# ---------------------------------------------------------------------------
# Briefing helpers (deterministic, no-LLM pre-turn context skim)
# ---------------------------------------------------------------------------

# Lifecycle filter: only ``stable`` concepts surface in a briefing by
# default. ``draft``/``deprecated`` are deliberately excluded so the
# pre-turn skim does not inject half-baked or retired material into a
# downstream mode's context. Callers can override via ``include_status``.
_BRIEFING_DEFAULT_STATUSES: frozenset[str] = frozenset({"stable"})


def _stale_after_is_past(stale_after: str, *, today: str | None = None) -> bool:
    """Return True if a YYYY-MM-DD ``stale_after`` date is in the past."""
    if not stale_after:
        return False
    sa = str(stale_after).strip()
    if len(sa) != 10 or sa[4] != "-" or sa[7] != "-":
        return False  # malformed → treat as not stale (don't drop on a guess)
    ref = today or datetime.now(UTC).strftime("%Y-%m-%d")
    return sa < ref


def score_concept_relevance(
    concept: dict[str, object],
    query: str,
    *,
    mode_intent_tags: list[str] | None = None,
) -> int:
    """Score how relevant a concept is to a user query + active mode.

    Deterministic token-overlap scoring. No LLM, no embeddings. The
    score is a rough ranking, not a probability; callers use it only
    to decide which concepts to surface and in what order.

    Inputs are taken from a concept dict with ``type``, ``title``,
    ``description``, ``tags`` keys (the shape ``okf_ingest`` returns).

    Args:
        concept: one concept document as a dict.
        query: the user's turn query (lowercased internally).
        mode_intent_tags: the active mode's intent tags, used to boost
            concepts whose ``tags`` overlap the mode's focus.

    Returns:
        An integer score. Zero means "no overlap" — the concept is
        irrelevant to this query and should not surface.
    """
    q = (query or "").lower()
    if not q:
        return 0

    # Tokenize on non-alphanumeric runs; drop very short tokens.
    import re

    query_tokens = {t for t in re.split(r"[^a-z0-9]+", q) if len(t) >= 3}
    if not query_tokens:
        return 0

    haystack_parts = [
        str(concept.get("title", "") or "").lower(),
        str(concept.get("description", "") or "").lower(),
        str(concept.get("type", "") or "").lower(),
    ]
    tags_val = concept.get("tags")
    if isinstance(tags_val, list):
        haystack_parts.extend(str(t).lower() for t in tags_val if t)
    haystack = " ".join(haystack_parts)
    haystack_tokens = set(re.split(r"[^a-z0-9]+", haystack)) if haystack else set()

    score = len(query_tokens & haystack_tokens)

    # Mode-intent tag overlap bonus: a concept tagged for the active
    # mode's focus gets a small boost even if the query tokens don't
    # overlap, because it is likely useful background for that mode.
    if mode_intent_tags:
        mode_tags_lower = {t.lower() for t in mode_intent_tags}
        if isinstance(tags_val, list):
            if any(str(t).lower() in mode_tags_lower for t in tags_val):
                score += 1

    return score


def filter_briefing_concepts(
    concepts: list[dict[str, object]],
    *,
    query: str,
    mode_intent_tags: list[str] | None = None,
    include_statuses: frozenset[str] | None = None,
    today: str | None = None,
    min_score: int = 1,
) -> list[dict[str, object]]:
    """Filter + rank concepts for a pre-turn briefing.

    Returns concepts that are (a) in an allowed lifecycle status
    (default ``stable``), (b) not past their ``stale_after`` date, and
    (c) score at least ``min_score`` against the query/mode. Sorted by
    descending score so callers can take the top N.
    """
    statuses = include_statuses or _BRIEFING_DEFAULT_STATUSES
    kept: list[tuple[int, dict[str, object]]] = []
    for concept in concepts:
        status = str(concept.get("status", "stable") or "stable")
        if status not in statuses:
            continue
        if _stale_after_is_past(str(concept.get("stale_after", "") or ""), today=today):
            continue
        score = score_concept_relevance(concept, query, mode_intent_tags=mode_intent_tags)
        if score >= min_score:
            kept.append((score, concept))
    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in kept]


def render_briefing(
    concepts: list[dict[str, object]],
    *,
    bundle_path: str,
    max_bullets: int = 5,
    max_bytes: int = 1500,
) -> str:
    """Render a compact, cited briefing markdown block from concepts.

    Returns the empty string when there are no concepts to surface
    (the host then renders nothing — no context pollution).

    Each bullet cites the concept's in-bundle path so the model can
    call ``read_file`` (or ``okf_ingest``) for the full text if the
    one-line skim looks relevant. The path is relative to the bundle
    root so the model can construct the full sandbox path as
    ``{bundle_path}/{concept_path}``.
    """
    if not concepts:
        return ""

    bullets: list[str] = []
    total = 0
    header = "**OKF working context:**"
    # Header + trailing newline cost.
    total += len(header) + 1

    for concept in concepts[:max_bullets]:
        path = str(concept.get("path", "") or "")
        title = str(concept.get("title", "") or "") or path
        desc = str(concept.get("description", "") or "")
        line = f"- `{path}` — {title}"
        if desc:
            line += f": {desc}"
        line += f" _(bundle: {bundle_path})_"
        if total + len(line) + 1 > max_bytes:
            break
        bullets.append(line)
        total += len(line) + 1

    if not bullets:
        return ""
    return header + "\n" + "\n".join(bullets)
