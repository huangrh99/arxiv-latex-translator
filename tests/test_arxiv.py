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


def test_fetch_title_reads_entry_title_from_arxiv_api() -> None:
    import io

    from arxiv_latex_translator.arxiv import fetch_title, parse_arxiv_input

    feed = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv Query: id_list=2611.12426</title>
  <entry><title>dNaViT: Discrete Codes are
      General-Purpose Visual Representations</title></entry>
</feed>"""
    requested = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout):
        requested.append(request.full_url)
        return Response(feed)

    title = fetch_title(parse_arxiv_input("2611.12426v1"), user_agent="test", opener=opener)
    assert title == "dNaViT: Discrete Codes are General-Purpose Visual Representations"
    assert "id_list=2611.12426" in requested[0]


def test_fetch_title_returns_empty_string_on_failure() -> None:
    from arxiv_latex_translator.arxiv import fetch_title, parse_arxiv_input

    def opener(request, timeout):
        raise OSError("offline")

    assert fetch_title(parse_arxiv_input("2611.12426"), user_agent="test", opener=opener) == ""
