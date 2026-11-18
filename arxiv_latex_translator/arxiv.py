"""arXiv identifier parsing, source download, and safe extraction."""

from __future__ import annotations

import gzip
import io
import os
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen


class ArxivInputError(ValueError):
    """Raised when an input cannot be interpreted as an arXiv identifier."""


@dataclass(frozen=True)
class ArxivIdentifier:
    value: str

    @property
    def url_id(self) -> str:
        return self.value

    @property
    def base_id(self) -> str:
        return re.sub(r"v\d+$", "", self.value)


_ARXIV_ID_RE = re.compile(
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-zA-Z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?)"
)


def parse_arxiv_input(value: str) -> ArxivIdentifier:
    """Parse a bare ID, arXiv URL, or ``arxiv:`` reference."""

    candidate = value.strip()
    match = _ARXIV_ID_RE.search(candidate)
    if not match:
        raise ArxivInputError("请输入有效的 arXiv 链接或论文 ID")
    return ArxivIdentifier(match.group("id"))


def download_source(
    identifier: ArxivIdentifier,
    destination: Path,
    *,
    user_agent: str = "arxiv-latex-translator/0.1",
    timeout: float = 60.0,
    opener: Callable[..., object] = urlopen,
) -> Path:
    """Download an arXiv e-print archive and write it to ``destination``."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(os.environ.get("ARXIV_MAX_DOWNLOAD_BYTES", str(200 * 1024 * 1024)))
    url = f"https://export.arxiv.org/e-print/{identifier.url_id}"
    request = Request(url, headers={"User-Agent": user_agent})
    try:
        with opener(request, timeout=timeout) as response:
            total = 0
            with destination.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(f"arXiv 源码超过大小限制（{max_bytes} 字节）")
                    stream.write(chunk)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"下载 arXiv 源码失败：{exc}") from exc
    if total == 0:
        raise RuntimeError("arXiv 源码下载结果为空")
    return destination


def _safe_member_path(root: Path, member_name: str) -> Path:
    relative = Path(member_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"源码归档包含不安全路径：{member_name}")
    target = (root / relative).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise RuntimeError(f"源码归档包含不安全路径：{member_name}")
    return target


def _write_member(stream, target: Path, state: dict[str, int], max_bytes: int) -> None:
    state["files"] += 1
    max_files = int(os.environ.get("ARXIV_MAX_EXTRACTED_FILES", "10000"))
    if state["files"] > max_files:
        raise RuntimeError("源码归档文件数量超过限制")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            state["bytes"] += len(chunk)
            if state["bytes"] > max_bytes:
                raise RuntimeError(f"解压后的源码超过大小限制（{max_bytes} 字节）")
            output.write(chunk)


def _extract_tar(data: bytes, root: Path, max_bytes: int) -> None:
    state = {"files": 0, "bytes": 0}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            target = _safe_member_path(root, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                _write_member(stream, target, state, max_bytes)


def _extract_zip(data: bytes, root: Path, max_bytes: int) -> None:
    state = {"files": 0, "bytes": 0}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            target = _safe_member_path(root, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                with archive.open(member) as stream:
                    _write_member(stream, target, state, max_bytes)


def extract_source(archive_path: Path, destination: Path) -> Path:
    """Extract a tar, zip, gzip, or plain TeX source into a fresh directory."""

    destination.mkdir(parents=True, exist_ok=True)
    max_archive_bytes = int(os.environ.get("ARXIV_MAX_ARCHIVE_BYTES", str(200 * 1024 * 1024)))
    max_extracted_bytes = int(os.environ.get("ARXIV_MAX_EXTRACTED_BYTES", str(512 * 1024 * 1024)))
    if archive_path.stat().st_size > max_archive_bytes:
        raise RuntimeError(f"源码归档超过大小限制（{max_archive_bytes} 字节）")
    data = archive_path.read_bytes()
    if tarfile.is_tarfile(archive_path):
        _extract_tar(data, destination, max_extracted_bytes)
    elif zipfile.is_zipfile(archive_path):
        _extract_zip(data, destination, max_extracted_bytes)
    else:
        try:
            decompressor = gzip.GzipFile(fileobj=io.BytesIO(data))
            parts: list[bytes] = []
            total = 0
            while True:
                chunk = decompressor.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_extracted_bytes:
                    raise RuntimeError("压缩后的源码超过大小限制")
                parts.append(chunk)
            decompressed = b"".join(parts)
        except OSError:
            decompressed = data
        if decompressed != data and tarfile.is_tarfile(io.BytesIO(decompressed)):
            _extract_tar(decompressed, destination, max_extracted_bytes)
        else:
            if len(decompressed) > max_extracted_bytes:
                raise RuntimeError("源码超过解压大小限制")
            target = destination / "main.tex"
            target.write_bytes(decompressed)
    return destination


def find_main_tex(root: Path) -> Path:
    """Find the most likely document root in an extracted source tree."""

    tex_files = sorted(root.rglob("*.tex"))
    if not tex_files:
        raise RuntimeError("源码归档中没有找到 .tex 文件")
    for path in tex_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"\\documentclass(?:\[[^]]*\])?\s*\{", text):
            return path
    return max(tex_files, key=lambda path: path.stat().st_size)
