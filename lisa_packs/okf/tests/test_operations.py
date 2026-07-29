"""Tests for the OKF pack operations.

Exercises the deterministic operations directly against temporary
filesystem bundles — no MCP server, no LLM, no storage sandbox.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lisa_packs.okf.operations.okf import (
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


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return tmp_path / "okf-bundle"


# ---------------------------------------------------------------------------
# okf_init
# ---------------------------------------------------------------------------


def test_init_creates_index_and_log(bundle):
    result = _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "My Bundle"})))
    assert "OKF bundle initialized" in result.output_text
    assert (bundle / "index.md").exists()
    assert (bundle / "log.md").exists()
    log_text = (bundle / "log.md").read_text(encoding="utf-8")
    assert "scaffold" in log_text
    assert "My Bundle" in log_text


def test_init_refuses_non_empty(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "First"})))
    result = _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "Second"})))
    assert "already exists" in result.output_text


def test_init_force_overwrites_non_empty(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "First"})))
    result = _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "Second", "force": True})))
    assert "OKF bundle initialized" in result.output_text


# ---------------------------------------------------------------------------
# okf_validate
# ---------------------------------------------------------------------------


def test_validate_passes_conformant_bundle(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "B"})))
    _write_concept(bundle, "modes/learn.md", type_str="Lisa Mode", title="Learn")
    result = _run(okf_validate.execute(_ctx({"bundle_path": str(bundle)})))
    assert "PASSED" in result.output_text
    assert result.output_json["conformant"] is True


def test_validate_fails_missing_type(bundle):
    bundle.mkdir()
    (bundle / "no_type.md").write_text("---\ntitle: No Type\n---\nbody", encoding="utf-8")
    result = _run(okf_validate.execute(_ctx({"bundle_path": str(bundle)})))
    assert "FAILED" in result.output_text
    assert not result.output_json["conformant"]


def test_validate_strict_catches_bad_status(bundle):
    bundle.mkdir()
    _write_concept(bundle, "c.md", type_str="Concept", status="garbage")
    result = _run(okf_validate.execute(_ctx({"bundle_path": str(bundle), "strict": True})))
    # Non-strict conformant stays True; strict violation is reported
    assert result.output_json["conformant"] is True
    assert any("status" in v["violation"] for v in result.output_json["violations"])


def test_validate_strict_include_strict_marks_nonconformant(bundle):
    bundle.mkdir()
    _write_concept(bundle, "c.md", type_str="Concept", status="garbage")
    result = _run(okf_validate.execute(
        _ctx({"bundle_path": str(bundle), "strict": True, "include_strict": True}),
    ))
    assert result.output_json["conformant"] is False


def test_validate_strict_catches_bad_stale_after(bundle):
    bundle.mkdir()
    _write_concept(bundle, "c.md", type_str="Concept", stale_after="not-a-date")
    result = _run(okf_validate.execute(_ctx({"bundle_path": str(bundle), "strict": True})))
    assert any("stale_after" in v["violation"] for v in result.output_json["violations"])


def test_validate_strict_catches_missing_generated_at(bundle):
    bundle.mkdir()
    # Write frontmatter with generated but no at
    fm = "---\ntype: Concept\ngenerated:\n  by: human:adam\n---\nbody\n"
    (bundle / "c.md").write_text(fm, encoding="utf-8")
    result = _run(okf_validate.execute(_ctx({"bundle_path": str(bundle), "strict": True})))
    assert any("generated.at" in v["violation"] for v in result.output_json["violations"])


def test_validate_strict_catches_empty_source_resource(bundle):
    bundle.mkdir()
    fm = (
        "---\n"
        "type: Concept\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: ''\n"
        "---\nbody\n"
    )
    (bundle / "c.md").write_text(fm, encoding="utf-8")
    result = _run(okf_validate.execute(_ctx({"bundle_path": str(bundle), "strict": True})))
    assert any("sources[0].resource" in v["violation"] for v in result.output_json["violations"])


# ---------------------------------------------------------------------------
# okf_ingest
# ---------------------------------------------------------------------------


def test_ingest_truncates_body_by_default(bundle):
    bundle.mkdir()
    long_body = "x" * 5000
    _write_concept(bundle, "c.md", type_str="Concept", body=long_body)
    result = _run(okf_ingest.execute(_ctx({"bundle_path": str(bundle)})))
    concept = result.output_json["concepts"][0]
    assert len(concept["body"]) == 2000


def test_ingest_full_body_when_truncate_disabled(bundle):
    bundle.mkdir()
    long_body = "x" * 5000
    _write_concept(bundle, "c.md", type_str="Concept", body=long_body)
    result = _run(okf_ingest.execute(_ctx({"bundle_path": str(bundle), "truncate_body": False})))
    concept = result.output_json["concepts"][0]
    assert len(concept["body"]) == 5000


# ---------------------------------------------------------------------------
# okf_reindex
# ---------------------------------------------------------------------------


def test_reindex_generates_index_with_concepts(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "B"})))
    _write_concept(bundle, "modes/learn.md", type_str="Lisa Mode", title="Learn")
    _write_concept(bundle, "playbooks/deploy.md", type_str="Playbook", title="Deploy")
    result = _run(okf_reindex.execute(_ctx({"bundle_path": str(bundle)})))
    assert "2 concepts" in result.output_text
    index_text = (bundle / "index.md").read_text(encoding="utf-8")
    assert "modes/learn.md" in index_text
    assert "playbooks/deploy.md" in index_text
    assert "Lisa Mode" in index_text


def test_reindex_appends_log_entry(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "B"})))
    _write_concept(bundle, "c.md", type_str="Concept")
    _run(okf_reindex.execute(_ctx({"bundle_path": str(bundle)})))
    log_text = (bundle / "log.md").read_text(encoding="utf-8")
    assert "reindex" in log_text


def test_reindex_preserves_existing_title(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "Original"})))
    _write_concept(bundle, "c.md", type_str="Concept")
    _run(okf_reindex.execute(_ctx({"bundle_path": str(bundle)})))
    # Re-reindex without title override — title should persist
    _run(okf_reindex.execute(_ctx({"bundle_path": str(bundle)})))
    index_text = (bundle / "index.md").read_text(encoding="utf-8")
    assert "Original" in index_text


def test_reindex_title_override(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "Original"})))
    _write_concept(bundle, "c.md", type_str="Concept")
    _run(okf_reindex.execute(_ctx({"bundle_path": str(bundle), "title": "Override"})))
    index_text = (bundle / "index.md").read_text(encoding="utf-8")
    assert "Override" in index_text


# ---------------------------------------------------------------------------
# okf_log
# ---------------------------------------------------------------------------


def test_log_appends_entry(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "B"})))
    result = _run(okf_log.execute(_ctx({
        "bundle_path": str(bundle),
        "action": "reviewed",
        "concept_path": "modes/learn.md",
        "by": "human:adam",
        "note": "looked good",
    })))
    assert "OKF log entry appended" in result.output_text
    log_text = (bundle / "log.md").read_text(encoding="utf-8")
    assert "reviewed" in log_text
    assert "modes/learn.md" in log_text
    assert "human:adam" in log_text
    assert "looked good" in log_text


def test_log_requires_action(bundle):
    _run(okf_init.execute(_ctx({"bundle_path": str(bundle), "title": "B"})))
    result = _run(okf_log.execute(_ctx({"bundle_path": str(bundle), "action": ""})))
    assert "action is required" in result.output_text


def test_log_creates_log_md_if_missing(tmp_path):
    bundle = tmp_path / "no-init-bundle"
    bundle.mkdir()
    result = _run(okf_log.execute(_ctx({
        "bundle_path": str(bundle),
        "action": "imported",
        "note": "from notion",
    })))
    assert "OKF log entry appended" in result.output_text
    log_text = (bundle / "log.md").read_text(encoding="utf-8")
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
# Helpers
# ---------------------------------------------------------------------------


def _write_concept(
    bundle: Path,
    rel_path: str,
    *,
    type_str: str = "Concept",
    title: str = "",
    description: str = "",
    status: str = "",
    stale_after: str = "",
    body: str = "body text",
) -> None:
    """Write a minimal OKF concept file directly to disk."""
    path = bundle / rel_path
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
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(body)
    path.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")