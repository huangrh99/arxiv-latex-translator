from pathlib import Path

import pytest

from arxiv_latex_translator.arxiv import ArxivInputError, extract_source, find_main_tex, parse_arxiv_input


def test_parse_arxiv_urls_and_versions() -> None:
    assert parse_arxiv_input("https://arxiv.org/abs/2401.12345v2").value == "2401.12345v2"
    assert parse_arxiv_input("arxiv:cs/0601001").value == "cs/0601001"
    assert parse_arxiv_input("2401.12345").base_id == "2401.12345"


def test_parse_rejects_invalid_input() -> None:
    with pytest.raises(ArxivInputError):
        parse_arxiv_input("https://example.com/paper.pdf")


def test_extract_source_and_find_document_root(tmp_path: Path) -> None:
    import io
    import tarfile

    archive_path = tmp_path / "source.tar"
    with tarfile.open(archive_path, "w") as archive:
        for name, content in {
            "paper/parts/intro.tex": "\\section{Intro}",
            "paper/main.tex": "\\documentclass{article}\n\\begin{document}\nHi\\end{document}",
        }.items():
            info = tarfile.TarInfo(name)
            data = content.encode()
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    extracted = extract_source(archive_path, tmp_path / "extracted")
    assert find_main_tex(extracted).name == "main.tex"
