"""OKF pack for Lisa.

Provides Open Knowledge Format (OKF) v0.2 ingestion, validation, emission,
and visualization. OKF is Google's open specification for representing
knowledge as a directory of markdown files with YAML frontmatter —
human-readable, agent-parseable, and version-controllable.

This pack does NOT replace the memory backend (SQLite). It is the
interchange layer: what you ship, share, and version-control.
"""

from __future__ import annotations

from pathlib import Path


def get_content_root() -> Path:
    """Return the content root directory for this pack."""
    return Path(__file__).parent
