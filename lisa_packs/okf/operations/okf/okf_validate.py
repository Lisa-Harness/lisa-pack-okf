"""OKF validate operation — check a directory for OKF v0.2 conformance.

No-LLM deterministic check: walks a directory tree, parses YAML frontmatter
from every non-reserved .md file, and verifies the single required field
(``type``) is present and non-empty. Returns a structured conformance report.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from kernel.models import OperationExecutionContext, OperationResult

logger = logging.getLogger(__name__)

# OKF v0.2 reserved filenames (§3.1 of the spec)
_RESERVED_FILENAMES = frozenset({"index.md", "log.md"})


class Parameters(BaseModel):
    """User-supplied inputs for the OKF validate operation."""

    bundle_path: str = Field(
        ...,
        description="Absolute path to the OKF bundle directory to validate.",
    )
    strict: bool = Field(
        default=False,
        description=(
            "If True, additionally check: `status` is one of draft/stable/deprecated, "
            "`stale_after` is a valid YYYY-MM-DD date, `generated.at` is present and "
            "ISO 8601 shaped, and every `sources[].resource` is a non-empty string. "
            "Strict violations are reported but do not affect `conformant` unless "
            "`include_strict` is also True."
        ),
    )
    include_strict: bool = Field(
        default=False,
        description="If True, strict violations also mark the bundle non-conformant.",
    )


_VALID_STATUSES = frozenset({"draft", "stable", "deprecated"})


def _check_strict(frontmatter: dict[str, object]) -> list[str]:
    """Return strict-mode violations for a single concept frontmatter block.

    These are SHOULD-level checks beyond the MUST-level ``type`` requirement.
    """
    out: list[str] = []

    status = frontmatter.get("status")
    if status is not None and str(status) not in _VALID_STATUSES:
        out.append(
            f"`status` must be one of {sorted(_VALID_STATUSES)}; got '{status}'."
        )

    stale_after = frontmatter.get("stale_after")
    if stale_after is not None:
        sa = str(stale_after)
        try:
            datetime.strptime(sa, "%Y-%m-%d")
        except ValueError:
            out.append(f"`stale_after` must be YYYY-MM-DD; got '{sa}'.")

    generated = frontmatter.get("generated")
    if isinstance(generated, dict):
        at = generated.get("at")
        if not at or not str(at).strip():
            out.append("`generated.at` is missing or empty.")
    elif generated is not None:
        out.append("`generated` must be a mapping with `by` and `at` keys.")

    sources = frontmatter.get("sources")
    if isinstance(sources, list):
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                out.append(f"`sources[{i}]` must be a mapping.")
                continue
            resource = src.get("resource")
            if not resource or not str(resource).strip():
                out.append(f"`sources[{i}].resource` is missing or empty.")
    elif sources is not None:
        out.append("`sources` must be a list of mappings.")

    return out


def _check_reserved_structure(file_path: Path, kind: str) -> list[str]:
    """Best-effort structural check for index.md / log.md when present."""
    out: list[str] = []
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return out
    if kind == "index.md":
        if not text.startswith("---"):
            out.append("index.md is missing YAML frontmatter.")
        elif "# Bundle Index" not in text and "# " not in text:
            out.append("index.md body does not look like a bundle listing.")
    elif kind == "log.md":
        if not text.lstrip().startswith("#") and not text.startswith("-"):
            out.append("log.md does not start with a heading or an entry line.")
    return out


class ConceptViolation(BaseModel):
    """A single conformance violation for one concept document."""

    path: str = Field(..., description="Relative path of the concept file within the bundle.")
    violation: str = Field(..., description="Description of the conformance violation.")


class OkfConformanceReport(BaseModel):
    """Structured conformance report for an OKF bundle."""

    bundle_path: str = Field(..., description="The validated bundle directory path.")
    conformant: bool = Field(..., description="True if the bundle passes all OKF v0.2 conformance checks.")
    total_concepts: int = Field(0, description="Number of concept documents found.")
    violations: list[ConceptViolation] = Field(
        default_factory=list,
        description="List of conformance violations found.",
    )


def _parse_frontmatter(file_path: Path) -> dict[str, object] | None:
    """Parse YAML frontmatter from a markdown file.

    Returns the parsed dict, or None if no frontmatter block is found.
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None

    if not text.startswith("---"):
        return None

    # Find the closing ---
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

    if not isinstance(parsed, dict):
        return None

    return parsed


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Validate an OKF v0.2 bundle directory for conformance.

    OKF v0.2 conformance (§11 of the spec) requires:
    1. Every non-reserved .md file contains parseable YAML frontmatter.
    2. Every frontmatter block contains a non-empty ``type`` field.
    3. Reserved filenames (index.md, log.md) follow their structure when present
       (checked as a soft warning — consumers MUST NOT reject for missing index.md).

    When ``strict`` is True, additionally checks SHOULD-level fields:
    ``status`` enum, ``stale_after`` date format, ``generated.at`` presence,
    and ``sources[].resource`` shape. Strict violations are reported separately
    and only affect ``conformant`` if ``include_strict`` is True.
    """
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    if not bundle_path_str:
        return OperationResult(
            output_text="OKF validation failed: bundle_path is required.",
        )

    strict = bool(ctx.params.get("strict", False))
    include_strict = bool(ctx.params.get("include_strict", False))

    bundle = Path(bundle_path_str)
    if not bundle.is_dir():
        return OperationResult(
            output_text=f"OKF validation failed: path is not a directory: {bundle_path_str}",
        )

    violations: list[ConceptViolation] = []
    strict_violations: list[ConceptViolation] = []
    total_concepts = 0

    for md_file in sorted(bundle.rglob("*.md")):
        rel_path = str(md_file.relative_to(bundle)).replace("\\", "/")

        # Reserved filenames are exempt from the type-field requirement
        if md_file.name in _RESERVED_FILENAMES:
            if strict:
                strict_violations.extend(
                    ConceptViolation(path=rel_path, violation=v)
                    for v in _check_reserved_structure(md_file, md_file.name)
                )
            continue

        total_concepts += 1
        frontmatter = _parse_frontmatter(md_file)

        if frontmatter is None:
            violations.append(
                ConceptViolation(
                    path=rel_path,
                    violation="Missing or unparseable YAML frontmatter block.",
                )
            )
            continue

        type_value = frontmatter.get("type")
        if not type_value or not isinstance(type_value, str) or not type_value.strip():
            violations.append(
                ConceptViolation(
                    path=rel_path,
                    violation="Missing or empty required 'type' field in frontmatter.",
                )
            )

        if strict:
            for sv in _check_strict(frontmatter):
                strict_violations.append(ConceptViolation(path=rel_path, violation=sv))

    all_violations = violations + strict_violations
    conformant = len(violations) == 0 and (not include_strict or len(strict_violations) == 0)

    report = OkfConformanceReport(
        bundle_path=bundle_path_str,
        conformant=conformant,
        total_concepts=total_concepts,
        violations=all_violations,
    )

    if report.conformant:
        output = (
            f"OKF v0.2 conformance: PASSED\n"
            f"Bundle: {bundle_path_str}\n"
            f"Concepts: {total_concepts}\n"
            f"Violations: 0"
        )
    else:
        lines = [
            "OKF v0.2 conformance: FAILED",
            f"Bundle: {bundle_path_str}",
            f"Concepts: {total_concepts}",
            f"Violations: {len(violations)}",
            "",
        ]
        for v in violations:
            lines.append(f"  - {v.path}: {v.violation}")
        output = "\n".join(lines)

    return OperationResult(
        output_text=output,
        output_json=report.model_dump(),
    )
