from pathlib import Path

from arxiv_latex_translator.build_check import inspect_build

MISSING_BIB_LOG = """
Package natbib Warning: Citation `he2016deep' on page 3 undefined on input line 12.
Package natbib Warning: Citation `radford2021clip' on page 3 undefined on input line 12.
Package natbib Warning: Citation `radford2021clip' on page 4 undefined on input line 40.
Package natbib Warning: Citation `tschannen2025siglip2' on page 3 undefined on input line 13.
LaTeX Warning: There were undefined references.
"""

MISSING_BIB_BLG = """This is BibTeX, Version 0.99d
The top-level auxiliary file: arxiv.aux
I couldn't open database file paper.bib
---line 263 of file arxiv.aux
I found no database files---while reading file arxiv.aux
"""


def write_build(tmp_path: Path, log: str, blg: str | None = None) -> Path:
    root = tmp_path / "arxiv.tex"
    root.write_text("\\documentclass{article}")
    root.with_suffix(".log").write_text(log)
    if blg is not None:
        root.with_suffix(".blg").write_text(blg)
    return root


def test_missing_bibliography_database_is_a_blocking_issue_with_evidence(tmp_path: Path) -> None:
    (tmp_path / "arxiv.bbl").write_text("\\begin{thebibliography}{1}\\end{thebibliography}")
    issues = inspect_build(write_build(tmp_path, MISSING_BIB_LOG, MISSING_BIB_BLG))
    kinds = {issue.kind: issue for issue in issues}
    assert kinds["undefined_citations"].count == 3
    assert kinds["undefined_citations"].blocking
    assert "radford2021clip" in kinds["undefined_citations"].evidence
    assert kinds["bibtex_database_missing"].blocking
    assert "paper.bib" in kinds["bibtex_database_missing"].evidence
    assert "arxiv.bbl" in kinds["bibtex_database_missing"].evidence


def test_a_couple_of_undefined_references_only_warn(tmp_path: Path) -> None:
    log = "LaTeX Warning: Reference `fig:missing' on page 2 undefined on input line 5.\n"
    issues = inspect_build(write_build(tmp_path, log))
    assert [(issue.kind, issue.count, issue.blocking) for issue in issues] == [("undefined_references", 1, False)]


def test_missing_glyphs_are_reported_by_character(tmp_path: Path) -> None:
    log = "\n".join(["Missing character: There is no 鑫 (U+946B) in font [lmroman10-regular]:mapping=tex-text;!"] * 6)
    issues = inspect_build(write_build(tmp_path, log))
    assert issues[0].kind == "missing_glyphs" and issues[0].blocking and "鑫" in issues[0].evidence


def test_clean_build_has_no_issues(tmp_path: Path) -> None:
    assert inspect_build(write_build(tmp_path, "Output written on arxiv.pdf (12 pages).\n")) == []


def test_logs_from_an_earlier_build_are_ignored(tmp_path: Path) -> None:
    import os

    root = write_build(tmp_path, "Output written on arxiv.pdf (12 pages).\n", MISSING_BIB_BLG)
    os.utime(root.with_suffix(".blg"), (1000, 1000))
    assert inspect_build(root, since=2000) == []
