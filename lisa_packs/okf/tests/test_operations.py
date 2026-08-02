"""Tests for the OKF pack operations.

Every operation is exercised against a fake ``lisa-storage`` sandbox
backed by a real ``tmp_path`` directory — no MCP server, no LLM, no
host filesystem access outside the sandbox root. ``bundle_path`` is
always a sandbox-relative string (e.g. ``"okf/bundle"``), matching the
contract the pack now enforces.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lisa_packs.okf.operations.okf import (
    okf_briefing,
    okf_emit_concept,
    okf_export_memory,
    okf_ingest,
    okf_init,
    okf_log,
    okf_reindex,
    okf_validate,
)


def _ctx(params: dict, *, execute_tool_fn=None) -> object:
    """Build a minimal OperationExecutionContext stand-in for deterministic tests."""
    return SimpleNamespace(
        params=params,
        workspace_id="test-ws",
        user_id="test-user",
        user_slug="test-user",
        surface_id="test-surface",
        memory_scope="personal",
        mode_id="test",
        execute_tool_fn=execute_tool_fn,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


BUNDLE = "okf/bundle"


@pytest.fixture
def fake_tool(tmp_path: Path) -> tuple[object, Path]:
    """Storage fake + the sandbox root it operates against."""
    fake, _ = _storage_fake(tmp_path)
    return fake, tmp_path


# ---------------------------------------------------------------------------
# Storage fake (simulates lisa-storage write_file/read_file/list_files/make_dir)
# ---------------------------------------------------------------------------


def _storage_fake(sandbox_root: Path) -> tuple[object, dict]:
    """Build a fake execute_tool_fn backed by a real tmp_path sandbox.

    Simulates the ``lisa-storage`` ``write_file``/``read_file``/
    ``list_files``/``make_dir`` tools against a sandbox rooted at
    ``sandbox_root`` (the files root, not the bundle root). Returns
    (fake_fn, store) where ``store`` maps sandbox-relative path ->
    content for assertions.
    """
    store: dict[str, str] = {}

    async def fake_tool(tool_id, tool_ctx):
        params = tool_ctx.parameters
        if tool_id == "write_file":
            path = params["path"]
            full = sandbox_root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(params["content"], encoding="utf-8")
            store[path] = params["content"]
            return SimpleNamespace(output_text='{"ok": true}')
        if tool_id == "make_dir":
            path = params["path"]
            full = sandbox_root / path
            full.mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(output_text='{"ok": true}')
        if tool_id == "list_files":
            prefix = params.get("prefix", "").strip("/")
            search_root = sandbox_root / prefix if prefix else sandbox_root
            if not search_root.is_dir():
                return SimpleNamespace(output_text="[]")
            files = []
            for item in sorted(search_root.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(sandbox_root)
                    files.append(str(rel).replace("\\", "/"))
            return SimpleNamespace(output_text=json.dumps(files))
        if tool_id == "read_file":
            path = params["path"]
            full = sandbox_root / path
            if not full.is_file():
                return SimpleNamespace(output_text='{"error": "not found"}')
            return SimpleNamespace(output_text=full.read_text(encoding="utf-8"))
        return SimpleNamespace(output_text='{"error": "unknown tool"}')

    return fake_tool, store


def _write_concept(
    sandbox_root: Path,
    bundle_path: str,
    rel_path: str,
    *,
    type_str: str = "Concept",
    title: str = "",
    description: str = "",
    status: str = "",
    stale_after: str = "",
    resource: str = "",
    tags: list[str] | None = None,
    body: str = "body text",
) -> None:
    """Write a minimal OKF concept file directly into the sandbox."""
    path = sandbox_root / bundle_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---", f"type: {type_str}"]
    if title:
        fm_lines.append(f"title: {title}")
    if description:
        fm_lines.append(f"description: {description}")
    if status:
        fm_lines.append(f"status: {status}")
    if stale_after:
        fm_lines.append(f"stale_after: {stale_after}")
    if resource:
        fm_lines.append(f"resource: {resource}")
    if tags:
        fm_lines.append("tags: [" + ", ".join(tags) + "]")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(body)
    path.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")


def _read(sandbox_root: Path, bundle_path: str, rel_path: str) -> str:
    return (sandbox_root / bundle_path / rel_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# okf_init
# ---------------------------------------------------------------------------


def test_init_creates_index_and_log(fake_tool):
    fake, sandbox = fake_tool
    result = _run(okf_init.execute(_ctx(
        {"bundle_path": BUNDLE, "title": "My Bundle"},
        execute_tool_fn=fake,
    )))
    assert "OKF bundle initialized" in result.output_text
    index_text = _read(sandbox, BUNDLE, "index.md")
    log_text = _read(sandbox, BUNDLE, "log.md")
    assert "My Bundle" in index_text
    assert "scaffold" in log_text
    assert "My Bundle" in log_text


def test_init_refuses_non_empty(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "First"}, execute_tool_fn=fake)))
    # Add a stray concept so the bundle is non-empty.
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept")
    result = _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "Second"}, execute_tool_fn=fake)))
    assert "already exists" in result.output_text


def test_init_force_overwrites_non_empty(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "First"}, execute_tool_fn=fake)))
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept")
    result = _run(okf_init.execute(
        _ctx({"bundle_path": BUNDLE, "title": "Second", "force": True}, execute_tool_fn=fake),
    ))
    assert "OKF bundle initialized" in result.output_text


def test_init_rejects_parent_traversal(fake_tool):
    fake, _ = fake_tool
    result = _run(okf_init.execute(_ctx(
        {"bundle_path": "okf/../escape", "title": "B"}, execute_tool_fn=fake,
    )))
    assert "'..' segments" in result.output_text


def test_init_requires_storage():
    # No execute_tool_fn injected — init must fail gracefully, not raise.
    result = _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "B"})))
    assert "OKF init failed" in result.output_text
    assert "lisa-storage unavailable" in result.output_text


# ---------------------------------------------------------------------------
# okf_validate
# ---------------------------------------------------------------------------


def test_validate_passes_conformant_bundle(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "B"}, execute_tool_fn=fake)))
    _write_concept(sandbox, BUNDLE, "modes/learn.md", type_str="Lisa Mode", title="Learn")
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    assert "PASSED" in result.output_text
    assert result.output_json["conformant"] is True


def test_validate_fails_missing_type(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, BUNDLE, "no_type.md", type_str="")
    # _write_concept always writes a type line; rewrite it without type.
    (sandbox / BUNDLE / "no_type.md").write_text(
        "---\ntitle: No Type\n---\nbody", encoding="utf-8"
    )
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    assert "FAILED" in result.output_text
    assert not result.output_json["conformant"]


def test_validate_strict_catches_bad_status(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept", status="garbage")
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE, "strict": True}, execute_tool_fn=fake)))
    assert result.output_json["conformant"] is True
    assert any("status" in v["violation"] for v in result.output_json["violations"])


def test_validate_strict_include_strict_marks_nonconformant(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept", status="garbage")
    result = _run(okf_validate.execute(
        _ctx({"bundle_path": BUNDLE, "strict": True, "include_strict": True}, execute_tool_fn=fake),
    ))
    assert result.output_json["conformant"] is False


def test_validate_strict_catches_bad_stale_after(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept", stale_after="not-a-date")
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE, "strict": True}, execute_tool_fn=fake)))
    assert any("stale_after" in v["violation"] for v in result.output_json["violations"])


def test_validate_strict_catches_missing_generated_at(fake_tool):
    fake, sandbox = fake_tool
    fm = "---\ntype: Concept\ngenerated:\n  by: human:adam\n---\nbody\n"
    (sandbox / BUNDLE).mkdir(parents=True, exist_ok=True)
    (sandbox / BUNDLE / "c.md").write_text(fm, encoding="utf-8")
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE, "strict": True}, execute_tool_fn=fake)))
    assert any("generated.at" in v["violation"] for v in result.output_json["violations"])


def test_validate_strict_catches_empty_source_resource(fake_tool):
    fake, sandbox = fake_tool
    fm = (
        "---\n"
        "type: Concept\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: ''\n"
        "---\nbody\n"
    )
    (sandbox / BUNDLE).mkdir(parents=True, exist_ok=True)
    (sandbox / BUNDLE / "c.md").write_text(fm, encoding="utf-8")
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE, "strict": True}, execute_tool_fn=fake)))
    assert any("sources[0].resource" in v["violation"] for v in result.output_json["violations"])


# ---------------------------------------------------------------------------
# okf_validate — strict duplicate-resource detection
# ---------------------------------------------------------------------------


def test_validate_strict_flags_duplicate_resource(fake_tool):
    fake, sandbox = fake_tool
    (sandbox / BUNDLE).mkdir(parents=True, exist_ok=True)
    (sandbox / BUNDLE / "a.md").write_text(
        "---\ntype: Concept\nresource: self://x/one\n---\nbody\n", encoding="utf-8"
    )
    (sandbox / BUNDLE / "b.md").write_text(
        "---\ntype: Concept\nresource: self://x/one\n---\nbody\n", encoding="utf-8"
    )
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE, "strict": True}, execute_tool_fn=fake)))
    dupes = [v for v in result.output_json["violations"] if "Duplicate 'resource'" in v["violation"]]
    assert len(dupes) == 1
    assert "self://x/one" in dupes[0]["violation"]
    assert "a.md" in dupes[0]["violation"]


def test_validate_non_strict_ignores_duplicate_resource(fake_tool):
    fake, sandbox = fake_tool
    (sandbox / BUNDLE).mkdir(parents=True, exist_ok=True)
    (sandbox / BUNDLE / "a.md").write_text(
        "---\ntype: Concept\nresource: self://x/one\n---\nbody\n", encoding="utf-8"
    )
    (sandbox / BUNDLE / "b.md").write_text(
        "---\ntype: Concept\nresource: self://x/one\n---\nbody\n", encoding="utf-8"
    )
    result = _run(okf_validate.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    assert result.output_json["conformant"] is True


# ---------------------------------------------------------------------------
# okf_ingest
# ---------------------------------------------------------------------------


def test_ingest_truncates_body_by_default(fake_tool):
    fake, sandbox = fake_tool
    long_body = "x" * 5000
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept", body=long_body)
    result = _run(okf_ingest.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    concept = result.output_json["concepts"][0]
    assert len(concept["body"]) == 2000


def test_ingest_full_body_when_truncate_disabled(fake_tool):
    fake, sandbox = fake_tool
    long_body = "x" * 5000
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept", body=long_body)
    result = _run(okf_ingest.execute(
        _ctx({"bundle_path": BUNDLE, "truncate_body": False}, execute_tool_fn=fake),
    ))
    concept = result.output_json["concepts"][0]
    assert len(concept["body"]) == 5000


def test_ingest_empty_bundle_returns_no_concepts(fake_tool):
    fake, _ = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "B"}, execute_tool_fn=fake)))
    result = _run(okf_ingest.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    assert "no concepts found" in result.output_text
    assert result.output_json["concepts"] == []


# ---------------------------------------------------------------------------
# okf_reindex
# ---------------------------------------------------------------------------


def test_reindex_generates_index_with_concepts(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "B"}, execute_tool_fn=fake)))
    _write_concept(sandbox, BUNDLE, "modes/learn.md", type_str="Lisa Mode", title="Learn")
    _write_concept(sandbox, BUNDLE, "playbooks/deploy.md", type_str="Playbook", title="Deploy")
    result = _run(okf_reindex.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    assert "2 concepts" in result.output_text
    index_text = _read(sandbox, BUNDLE, "index.md")
    assert "modes/learn.md" in index_text
    assert "playbooks/deploy.md" in index_text
    assert "Lisa Mode" in index_text


def test_reindex_appends_log_entry(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "B"}, execute_tool_fn=fake)))
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept")
    _run(okf_reindex.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    log_text = _read(sandbox, BUNDLE, "log.md")
    assert "reindex" in log_text


def test_reindex_preserves_existing_title(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "Original"}, execute_tool_fn=fake)))
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept")
    _run(okf_reindex.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    _run(okf_reindex.execute(_ctx({"bundle_path": BUNDLE}, execute_tool_fn=fake)))
    index_text = _read(sandbox, BUNDLE, "index.md")
    assert "Original" in index_text


def test_reindex_title_override(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "Original"}, execute_tool_fn=fake)))
    _write_concept(sandbox, BUNDLE, "c.md", type_str="Concept")
    _run(okf_reindex.execute(_ctx({"bundle_path": BUNDLE, "title": "Override"}, execute_tool_fn=fake)))
    index_text = _read(sandbox, BUNDLE, "index.md")
    assert "Override" in index_text


# ---------------------------------------------------------------------------
# okf_log
# ---------------------------------------------------------------------------


def test_log_appends_entry(fake_tool):
    fake, sandbox = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "B"}, execute_tool_fn=fake)))
    result = _run(okf_log.execute(_ctx({
        "bundle_path": BUNDLE,
        "action": "reviewed",
        "concept_path": "modes/learn.md",
        "by": "human:adam",
        "note": "looked good",
    }, execute_tool_fn=fake)))
    assert "OKF log entry appended" in result.output_text
    log_text = _read(sandbox, BUNDLE, "log.md")
    assert "reviewed" in log_text
    assert "modes/learn.md" in log_text
    assert "human:adam" in log_text
    assert "looked good" in log_text


def test_log_requires_action(fake_tool):
    fake, _ = fake_tool
    _run(okf_init.execute(_ctx({"bundle_path": BUNDLE, "title": "B"}, execute_tool_fn=fake)))
    result = _run(okf_log.execute(_ctx({"bundle_path": BUNDLE, "action": ""}, execute_tool_fn=fake)))
    assert "action is required" in result.output_text


def test_log_creates_log_md_if_missing(fake_tool):
    fake, sandbox = fake_tool
    # No okf_init — bundle dir exists but log.md does not.
    (sandbox / BUNDLE).mkdir(parents=True, exist_ok=True)
    result = _run(okf_log.execute(_ctx({
        "bundle_path": BUNDLE,
        "action": "imported",
        "note": "from notion",
    }, execute_tool_fn=fake)))
    assert "OKF log entry appended" in result.output_text
    log_text = _read(sandbox, BUNDLE, "log.md")
    assert "imported" in log_text
    assert "# Bundle Log" in log_text


# ---------------------------------------------------------------------------
# okf_emit_concept (provenance params)
# ---------------------------------------------------------------------------


def test_emit_concept_accepts_provenance_params():
    """okf_emit_concept goes through lisa-storage write_file; here we verify
    the frontmatter it builds carries the new provenance fields."""
    captured: dict = {}

    async def fake_tool(tool_id, tool_ctx):
        captured["path"] = tool_ctx.parameters["path"]
        captured["content"] = tool_ctx.parameters["content"]
        return SimpleNamespace(output_text='{"ok": true}')

    result = _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "modes/learn.md",
            "type": "Lisa Mode",
            "title": "Learn",
            "description": "Learning mode",
            "tags": ["mode", "learn"],
            "status": "stable",
            "generated_by": "human:adam",
            "resource": "https://example.com/learn",
            "stale_after": "2026-12-31",
            "body": "Learn things.",
        },
        execute_tool_fn=fake_tool,
    )))
    assert "OKF concept emitted" in result.output_text
    content = captured["content"]
    assert "human:adam" in content
    assert "https://example.com/learn" in content
    assert "2026-12-31" in content


# ---------------------------------------------------------------------------
# okf_emit_concept — resource-as-identity dedup (2026-07-28)
# ---------------------------------------------------------------------------


def test_emit_concept_refuses_cross_path_duplicate_resource(tmp_path: Path):
    """A concept whose resource already exists at a different path is refused."""
    fake_tool, _ = _storage_fake(tmp_path)
    bundle_root = tmp_path / "okf" / "bundle"

    _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "analysis/first.md",
            "type": "Analysis",
            "title": "First",
            "resource": "self://lisa/self-assessment",
            "body": "first copy",
        },
        execute_tool_fn=fake_tool,
    )))

    result = _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "self-assessment.md",
            "type": "Analysis",
            "title": "Second",
            "resource": "self://lisa/self-assessment",
            "body": "second copy",
        },
        execute_tool_fn=fake_tool,
    )))
    assert "OKF emit failed" in result.output_text
    assert "already exists" in result.output_text
    assert "analysis/first.md" in result.output_text
    assert not (bundle_root / "self-assessment.md").exists()


def test_emit_concept_same_path_reemit_is_idempotent_update(tmp_path: Path):
    """Re-emitting the same resource at the SAME path is an update, not a dup."""
    fake_tool, _ = _storage_fake(tmp_path)
    concept_file = tmp_path / "okf" / "bundle" / "analysis" / "self.md"

    first = _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "analysis/self.md",
            "type": "Analysis",
            "title": "v1",
            "resource": "self://lisa/self-assessment",
            "body": "first",
        },
        execute_tool_fn=fake_tool,
    )))
    assert "OKF concept emitted" in first.output_text

    second = _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "analysis/self.md",
            "type": "Analysis",
            "title": "v2",
            "resource": "self://lisa/self-assessment",
            "body": "updated",
        },
        execute_tool_fn=fake_tool,
    )))
    assert "OKF concept emitted" in second.output_text
    assert concept_file.read_text(encoding="utf-8").count("updated") == 1


def test_emit_concept_no_dedup_when_resource_empty(tmp_path: Path):
    """An empty resource has no identity key — dedup is skipped, both writes land."""
    fake_tool, _ = _storage_fake(tmp_path)
    bundle_root = tmp_path / "okf" / "bundle"

    _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "a.md",
            "type": "Concept",
            "title": "A",
            "resource": "",
            "body": "a",
        },
        execute_tool_fn=fake_tool,
    )))
    result = _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "b.md",
            "type": "Concept",
            "title": "B",
            "resource": "",
            "body": "b",
        },
        execute_tool_fn=fake_tool,
    )))
    assert "OKF concept emitted" in result.output_text
    assert (bundle_root / "a.md").exists()
    assert (bundle_root / "b.md").exists()


def test_emit_concept_dedup_skipped_on_list_files_error(tmp_path: Path):
    """If list_files fails, dedup degrades to a no-op and the write proceeds."""
    sandbox_root = tmp_path

    async def fake_tool(tool_id, tool_ctx):
        if tool_id == "list_files":
            raise RuntimeError("storage unavailable")
        if tool_id == "write_file":
            path = tool_ctx.parameters["path"]
            full = sandbox_root / path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(tool_ctx.parameters["content"], encoding="utf-8")
            return SimpleNamespace(output_text='{"ok": true}')
        return SimpleNamespace(output_text='{"error": "unknown"}')

    result = _run(okf_emit_concept.execute(_ctx(
        {
            "bundle_path": "okf/bundle",
            "concept_path": "c.md",
            "type": "Concept",
            "title": "C",
            "resource": "self://x/c",
            "body": "c",
        },
        execute_tool_fn=fake_tool,
    )))
    assert "OKF concept emitted" in result.output_text


# ---------------------------------------------------------------------------
# okf_export_memory — smoke test that records are written via storage
# ---------------------------------------------------------------------------


def test_export_memory_writes_records_via_storage(tmp_path: Path):
    fake_tool, store = _storage_fake(tmp_path)

    async def memory_list(tool_id, tool_ctx):
        assert tool_id == "memory.list"
        return SimpleNamespace(output_text=json.dumps([
            {"id": "rec-1", "content": "Remember to deploy.", "memory_tier": "persistent"},
        ]))

    async def router(tool_id, tool_ctx):
        if tool_id == "memory.list":
            return await memory_list(tool_id, tool_ctx)
        return await fake_tool(tool_id, tool_ctx)

    result = _run(okf_export_memory.execute(_ctx(
        {"bundle_path": "okf/bundle", "limit": 10},
        execute_tool_fn=router,
    )))
    assert "exported" in result.output_text.lower()
    written = [p for p in store if p.startswith("okf/bundle/memories/") and p.endswith(".md")]
    assert len(written) == 1
    body = store[written[0]]
    assert "Remember to deploy." in body
    assert "type: Memory Record" in body


# ---------------------------------------------------------------------------
# okf_briefing — deterministic pre-turn context skim
# ---------------------------------------------------------------------------

WORKING = "okf/working"


def test_briefing_empty_when_no_bundle(fake_tool):
    fake, _ = fake_tool
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "how do I deploy"},
        execute_tool_fn=fake,
    )))
    assert result.output_text == ""
    assert result.output_json["surfaced_concepts"] == []


def test_briefing_empty_for_acknowledgement(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, WORKING, "deploy.md",
                   type_str="Playbook", title="Deploy", description="how to deploy")
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "ok"},
        execute_tool_fn=fake,
    )))
    assert result.output_text == ""
    assert result.output_json["surfaced_concepts"] == []


def test_briefing_surfaces_relevant_concept(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, WORKING, "playbooks/deploy.md",
                   type_str="Playbook", title="Deploy to staging",
                   description="Step-by-step deploy procedure",
                   tags=["deploy", "staging"])
    _write_concept(sandbox, WORKING, "modes/learn.md",
                   type_str="Lisa Mode", title="Learn", description="learning mode")
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "how do I deploy to staging"},
        execute_tool_fn=fake,
    )))
    assert result.output_text != ""
    assert "playbooks/deploy.md" in result.output_text
    assert "okf/working" in result.output_text
    assert result.output_json["surfaced_concepts"] == ["playbooks/deploy.md"]


def test_briefing_skips_irrelevant_concept(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, WORKING, "modes/learn.md",
                   type_str="Lisa Mode", title="Learn", description="learning mode")
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "deploy the database"},
        execute_tool_fn=fake,
    )))
    assert result.output_text == ""
    assert result.output_json["surfaced_concepts"] == []


def test_briefing_skips_deprecated_concept(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, WORKING, "playbooks/deploy.md",
                   type_str="Playbook", title="Deploy", description="how to deploy",
                   status="deprecated")
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "how do I deploy"},
        execute_tool_fn=fake,
    )))
    assert result.output_text == ""
    assert result.output_json["surfaced_concepts"] == []


def test_briefing_skips_stale_concept(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, WORKING, "playbooks/deploy.md",
                   type_str="Playbook", title="Deploy", description="how to deploy",
                   stale_after="2020-01-01")
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "how do I deploy", "today": "2026-08-02"},
        execute_tool_fn=fake,
    )))
    assert result.output_text == ""


def test_briefing_mode_intent_tag_boosts(fake_tool):
    fake, sandbox = fake_tool
    # Concept tagged 'write' matches markdown mode's intent tag, even
    # though the query tokens don't overlap.
    _write_concept(sandbox, WORKING, "notes/style-guide.md",
                   type_str="Guide", title="Style Guide", description="house style",
                   tags=["write", "markdown"])
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "revise the intro paragraph", "mode_intent_tags": ["write"]},
        execute_tool_fn=fake,
    )))
    assert "notes/style-guide.md" in result.output_text


def test_briefing_respects_max_bullets(fake_tool):
    fake, sandbox = fake_tool
    for i in range(5):
        _write_concept(sandbox, WORKING, f"playbooks/deploy-{i}.md",
                       type_str="Playbook", title=f"Deploy {i}", description="deploy")
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "deploy", "max_bullets": 2},
        execute_tool_fn=fake,
    )))
    assert len(result.output_json["surfaced_concepts"]) == 2


def test_briefing_respects_max_bytes(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, WORKING, "playbooks/deploy.md",
                   type_str="Playbook", title="Deploy", description="x" * 200)
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "deploy", "max_bytes": 50},
        execute_tool_fn=fake,
    )))
    assert result.output_text == ""


def test_briefing_default_bundle_path_is_okf_working(fake_tool):
    fake, sandbox = fake_tool
    _write_concept(sandbox, "okf/working", "playbooks/deploy.md",
                   type_str="Playbook", title="Deploy", description="how to deploy")
    result = _run(okf_briefing.execute(_ctx(
        {"query": "how do I deploy"},  # no bundle_path passed
        execute_tool_fn=fake,
    )))
    assert "playbooks/deploy.md" in result.output_text
    assert result.output_json["bundle_path"] == "okf/working"


def test_briefing_no_storage_injected_returns_empty_not_error():
    """A bare host without execute_tool_fn gets an empty briefing, not a crash."""
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "how do I deploy"},
    )))
    assert result.output_text == ""
    assert result.output_json["surfaced_concepts"] == []


def test_briefing_skips_reserved_filenames(fake_tool):
    fake, sandbox = fake_tool
    # index.md should not be surfaced even if it parses.
    (sandbox / WORKING).mkdir(parents=True, exist_ok=True)
    (sandbox / WORKING / "index.md").write_text(
        "---\ntype: OKF Bundle Index\ntitle: Working\n---\n# Working\n",
        encoding="utf-8",
    )
    _write_concept(sandbox, WORKING, "playbooks/deploy.md",
                   type_str="Playbook", title="Deploy", description="how to deploy")
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": WORKING, "query": "deploy or index"},
        execute_tool_fn=fake,
    )))
    assert "index.md" not in result.output_text
    assert "playbooks/deploy.md" in result.output_text


def test_briefing_rejects_parent_traversal():
    result = _run(okf_briefing.execute(_ctx(
        {"bundle_path": "../escape", "query": "deploy"},
        execute_tool_fn=lambda *_a: None,
    )))
    assert "may not contain '..'" in result.output_text