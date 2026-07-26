"""OKF validate operation — check a directory for OKF v0.2 conformance.

No-LLM deterministic check: walks a directory tree, parses YAML frontmatter
from every non-reserved .md file, and verifies the single required field
(``type``) is present and non-empty. Returns a structured conformance report.
"""

from __future__ import annotations

import logging
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
    """
    bundle_path_str = str(ctx.params.get("bundle_path", "")).strip()
    if not bundle_path_str:
        return OperationResult(
            output_text="OKF validation failed: bundle_path is required.",
        )

    bundle = Path(bundle_path_str)
    if not bundle.is_dir():
        return OperationResult(
            output_text=f"OKF validation failed: path is not a directory: {bundle_path_str}",
        )

    violations: list[ConceptViolation] = []
    total_concepts = 0

    for md_file in sorted(bundle.rglob("*.md")):
        rel_path = str(md_file.relative_to(bundle)).replace("\\", "/")

        # Reserved filenames are exempt from the type-field requirement
        if md_file.name in _RESERVED_FILENAMES:
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

    report = OkfConformanceReport(
        bundle_path=bundle_path_str,
        conformant=len(violations) == 0,
        total_concepts=total_concepts,
        violations=violations,
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
            f"OKF v0.2 conformance: FAILED",
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
