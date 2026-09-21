---
type: Lisa Mode
title: OKF Mode
description: Validate, ingest, emit, and export Open Knowledge Format (OKF) v0.2 bundles — the vendor-neutral standard for agent knowledge interchange.
tags: [okf, knowledge, interchange, standards]
status: stable
generated: { by: human:adam, at: 2026-07-25T00:00:00Z }
mode_id: okf
mode_provider_id: lisa-pack-okf
start_stage_id: none
mode_kind: chat
require_query: true
routable: true
priority: 8
fast_path_patterns:
  - "\\bokf\\b"
  - "\\bopen knowledge format\\b"
pre_turn_operation_ids: [okf_briefing]
routing_intent_tags: [okf]
satisfies_intents: [okf]
routing_intent_keywords:
  okf:
    - okf
    - open knowledge format
    - knowledge bundle
    - init okf bundle
    - validate okf
    - ingest okf
    - emit okf
    - reindex okf
    - log okf
    - export memory
    - okf concept
allowed_operation_ids: [okf_init, okf_validate, okf_ingest, okf_emit_concept, okf_export_memory, okf_reindex, okf_log, okf_briefing]
required_capabilities: [tools]
allowed_tool_ids: [memory.list, lisa-storage.write_file, lisa-storage.read_file, lisa-storage.list_files, lisa-storage.make_dir]
description: "OKF workflow mode for initializing, validating, ingesting, emitting, reindexing, logging, and exporting Open Knowledge Format bundles."
command_pattern: '^(?:(?:/)?(?:okf)(?:[\s,:;]+|(?=$)))(?P<query>.*)$'
---
# OKF Mode

Work with Open Knowledge Format (OKF) v0.2 bundles — directories of markdown
files with YAML frontmatter that represent knowledge in a vendor-neutral,
human-and-agent-friendly format.

All seven operations read and write exclusively through the `lisa-storage`
MCP server (`write_file`, `read_file`, `list_files`, `make_dir`). The pack
never touches the host filesystem directly; every `bundle_path` is a
sandbox-relative path under the host's `data_dir/files` root (e.g.
`okf/my-bundle`), not an absolute filesystem path. Parent traversal (`..`)
is rejected before the storage server re-validates.

## Available operations

Bundle lifecycle:

- `okf_init` — scaffold a conformant bundle directory with `index.md` + `log.md` stubs (`make_dir` + `write_file`)
- `okf_reindex` — regenerate `index.md` from the bundle's current concepts (`list_files` + `read_file` + `write_file`)
- `okf_log` — append a structured, timestamped entry to `log.md` (read-rewrite via `read_file` + `write_file`; no append primitive in `lisa-storage`)

Concept I/O:

- `okf_validate` — check a bundle for OKF v0.2 conformance (strict mode checks `status`, `stale_after`, `generated.at`, `sources[].resource`)
- `okf_ingest` — read an OKF bundle and return its concepts as structured data (full body when `truncate_body=False`)
- `okf_emit_concept` — write a single OKF concept document with full provenance (`generated_by`, `resource`, `stale_after`, `sources`) via `write_file`
- `okf_export_memory` — export memory records to an OKF bundle (calls `memory.list`, writes via `write_file`)

Working memory:

- `okf_briefing` — deterministic pre-turn skim of the working bundle (`okf/working` by default). Surfaces non-stale `stable` concepts whose `type`/`title`/`description`/`tags` overlap the user's query as a compact cited bullet list, or empty when nothing is relevant. Wired as a `pre_turn_operation_ids` entry on this mode, so the model gets a bounded advisory block of relevant concepts before the main turn — a skim, not a dump. Call `read_file` on a cited path for the full text.

## OKF v0.2 quick reference

Every concept document requires:

- `type` (required) — the kind of concept (e.g. `Lisa Mode`, `Playbook`, `Metric`)
- `title` (recommended) — human-readable display name
- `description` (recommended) — one-line summary
- `tags` (recommended) — YAML list of categorization strings

Trust and provenance fields (all optional):

- `generated: { by, at }` — who produced the content and when
- `verified: [{ by, at }]` — who confirmed it
- `status: draft | stable | deprecated`
- `stale_after: YYYY-MM-DD`
- `sources: [{ id, resource, title, ... }]`

Reserved filenames: `index.md` (directory listing), `log.md` (update history). `okf_init` scaffolds both; `okf_reindex` regenerates `index.md`; `okf_log` appends to `log.md`.
