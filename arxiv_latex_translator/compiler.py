"""LaTeX compilation with bounded subprocess execution."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
import re


KNOWN_PACKAGE_SHIMS = {
    "simpleicons": r"""\ProvidesPackage{simpleicons}[2026/11/19 local compatibility shim]
\newcommand{\simpleicon}[1]{}
""",
}


@dataclass(frozen=True)
class CompileResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    pdf_path: Path | None
    duration_seconds: float


def compiler_command(root: Path) -> list[str]:
    configured = os.environ.get("LATEX_COMMAND", "").strip()
    if configured:
        command = shlex.split(configured)
    elif shutil.which("latexmk"):
        command = ["latexmk", "-xelatex"]
    elif shutil.which("xelatex"):
        command = ["xelatex"]
    elif shutil.which("tectonic"):
        command = ["tectonic"]
    else:
        raise RuntimeError("没有找到 latexmk、xelatex 或 tectonic，请安装 LaTeX 工具链")
    if Path(command[0]).name == "tectonic":
        command.extend(["--keep-logs", root.name])
        return command
    if "-no-shell-escape" not in command:
        command.append("-no-shell-escape")
    command.extend(["-interaction=nonstopmode", "-halt-on-error", "-file-line-error"])
    if command[0].endswith("latexmk"):
        command.append(root.name)
    else:
        command.append(root.name)
    return command


def _ensure_cjk_support(root: Path) -> None:
    text = root.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"[\u3400-\u9fff]", text):
        return
    has_ctex = bool(re.search(r"\\usepackage(?:\[[^]]*\])?\{ctex\}", text) or re.search(r"\\documentclass(?:\[[^]]*\])?\{ctex", text))
    has_xecjk = bool(re.search(r"\\usepackage(?:\[[^]]*\])?\{xeCJK\}", text))
    updated = text
    if not has_ctex and not has_xecjk:
        match = re.search(r"\\documentclass(?:\[[^]]*\])?\{[^}]+\}", updated)
        if not match:
            raise RuntimeError("包含中文内容但找不到 documentclass，无法注入 CJK 支持")
        support = "\n\\usepackage{xeCJK}\n\\setCJKmainfont{Songti SC}\n"
        updated = updated[: match.end()] + support + updated[match.end() :]
    elif not has_ctex and "\\setCJKmainfont" not in updated:
        match = re.search(r"\\usepackage(?:\[[^]]*\])?\{xeCJK\}", updated)
        if match:
            updated = updated[: match.end()] + "\n\\setCJKmainfont{Songti SC}" + updated[match.end() :]
    if re.search(r"\\usepackage(?:\[[^]]*\])?\{microtype\}", updated) and "\\microtypesetup{activate=false}" not in updated:
        match = re.search(r"\\usepackage(?:\[[^]]*\])?\{microtype\}[^\n]*\n?", updated)
        if match:
            updated = updated[: match.end()] + "\\microtypesetup{activate=false}\n" + updated[match.end() :]
    if updated != text:
        root.write_text(updated, encoding="utf-8")


def _missing_packages(stderr: str) -> list[str]:
    return sorted(set(re.findall(r"File [`']([A-Za-z0-9_-]+)\.sty['`] not found", stderr)))


def _write_package_shims(root: Path, stderr: str) -> list[str]:
    created: list[str] = []
    for package in _missing_packages(stderr):
        content = KNOWN_PACKAGE_SHIMS.get(package)
        if content is None:
            continue
        shim = root.parent / f"{package}.sty"
        if not shim.exists():
            shim.write_text(content, encoding="utf-8")
            created.append(package)
    return created


def compile_latex(root: Path, artifact_dir: Path) -> CompileResult:
    _ensure_cjk_support(root)
    command = compiler_command(root)
    if re.search(r"[\u3400-\u9fff]", root.read_text(encoding="utf-8", errors="replace")) and "pdflatex" in Path(command[0]).name.lower():
        raise RuntimeError("包含中文内容时需要使用 XeLaTeX 或 LuaLaTeX 编译器")
    timeout = float(os.environ.get("LATEX_TIMEOUT_SECONDS", "300"))
    started = time.monotonic()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    returncode = 1
    for attempt in range(2):
        try:
            completed = subprocess.run(
                command,
                cwd=str(root.parent),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            timeout_stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stderr = timeout_stderr + f"\nLaTeX 编译超时（{timeout:.0f} 秒）"
        stdout_parts.append(stdout)
        stderr_parts.append(stderr)
        if returncode == 0:
            break
        if attempt == 0 and _write_package_shims(root, stderr):
            continue
        break
    stdout = "\n".join(stdout_parts)
    stderr = "\n".join(stderr_parts)
    duration = time.monotonic() - started
    produced_pdf = root.with_suffix(".pdf")
    pdf_path: Path | None = None
    if returncode == 0 and produced_pdf.exists():
        artifact_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = artifact_dir / "translated.pdf"
        shutil.copy2(produced_pdf, pdf_path)
    return CompileResult(tuple(command), returncode, stdout, stderr, pdf_path, duration)
