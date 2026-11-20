"""Post-compile quality checks.

A LaTeX build can exit with code 0 and still produce a broken PDF: undefined citations print as
`??`, missing glyphs print as blanks. These problems are warnings in the TeX log, so they are read
here deterministically from the build's `.log` and `.blg` files instead of inspecting the PDF.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Counts at or above these thresholds are worth an automatic repair. A few undefined references
# often exist in the original paper too, so smaller counts are reported as warnings only.
BLOCKING_UNDEFINED = 3
BLOCKING_GLYPHS = 5


@dataclass(frozen=True)
class BuildIssue:
    kind: str
    count: int
    blocking: bool
    summary: str
    evidence: str

    def describe(self) -> str:
        return f"{self.summary}\n  Evidence: {self.evidence}"


def _read(path: Path, since: float | None) -> str:
    """Read a build log, ignoring files left over from an earlier build."""

    try:
        if since is not None and path.stat().st_mtime < since:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _examples(values: list[str], limit: int = 8) -> str:
    unique = list(dict.fromkeys(values))
    shown = ", ".join(unique[:limit])
    return shown + (f", ... ({len(unique)} distinct)" if len(unique) > limit else "")


def inspect_build(root: Path, *, since: float | None = None) -> list[BuildIssue]:
    """Inspect the logs left next to `root` by the last compile and return the problems found.

    `since` is the compile start time; logs older than that belong to a previous build.
    """

    log = _read(root.with_suffix(".log"), since)
    blg = _read(root.with_suffix(".blg"), since)
    issues: list[BuildIssue] = []

    citations = re.findall(r"Citation [`'](.+?)' (?:on page \d+ )?undefined", log)
    if citations:
        keys = list(dict.fromkeys(citations))
        issues.append(
            BuildIssue(
                "undefined_citations",
                len(keys),
                len(keys) >= BLOCKING_UNDEFINED,
                f"{len(keys)} citation keys are undefined, so they print as ?? in the PDF.",
                _examples(keys),
            )
        )

    references = re.findall(r"Reference [`'](.+?)' on page \d+ undefined", log)
    if references:
        labels = list(dict.fromkeys(references))
        issues.append(
            BuildIssue(
                "undefined_references",
                len(labels),
                len(labels) >= BLOCKING_UNDEFINED,
                f"{len(labels)} cross-reference labels are undefined, so they print as ?? in the PDF.",
                _examples(labels),
            )
        )

    missing_databases = re.findall(r"I couldn't open database file (\S+)", blg)
    if missing_databases or "I found no database files" in blg:
        bbl_files = sorted(path.name for path in root.parent.glob("*.bbl"))
        evidence = f"BibTeX could not open {_examples(missing_databases) or 'any .bib database'}"
        if bbl_files:
            evidence += f"; prebuilt bibliography present: {', '.join(bbl_files)}"
        issues.append(
            BuildIssue(
                "bibtex_database_missing",
                len(missing_databases) or 1,
                True,
                "BibTeX ran without its .bib database and produced an empty bibliography.",
                evidence,
            )
        )

    glyphs = re.findall(r"Missing character: There is no (.+?) (?:\(U\+[0-9A-F]+\) )?in font", log)
    if glyphs:
        issues.append(
            BuildIssue(
                "missing_glyphs",
                len(glyphs),
                len(glyphs) >= BLOCKING_GLYPHS,
                f"{len(glyphs)} characters have no glyph in the selected font and render as blanks.",
                _examples(glyphs),
            )
        )
    return issues
