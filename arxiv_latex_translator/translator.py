"""Codex CLI translation adapter."""

from __future__ import annotations

import os
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class TranslationError(RuntimeError):
    """Raised when the configured translation command fails."""


@dataclass(frozen=True)
class CodexConfig:
    executable: str = "codex"
    model: str | None = None
    profile: str | None = None
    workdir: Path | None = None
    extra_args: tuple[str, ...] = ()
    timeout_seconds: float = 600.0

    @classmethod
    def from_env(cls) -> "CodexConfig":
        extra = tuple(shlex.split(os.environ.get("CODEX_EXTRA_ARGS", "")))
        try:
            timeout = float(os.environ.get("CODEX_TIMEOUT_SECONDS", "600"))
        except ValueError as exc:
            raise ValueError("CODEX_TIMEOUT_SECONDS 必须是数字") from exc
        if timeout <= 0:
            raise ValueError("CODEX_TIMEOUT_SECONDS 必须大于 0")
        workdir_value = os.environ.get("CODEX_WORKDIR", "").strip()
        return cls(
            executable=os.environ.get("CODEX_EXECUTABLE", "codex"),
            model=os.environ.get("CODEX_MODEL") or None,
            profile=os.environ.get("CODEX_PROFILE") or None,
            workdir=Path(workdir_value).expanduser() if workdir_value else None,
            extra_args=extra,
            timeout_seconds=timeout,
        )

    def cache_key(self) -> str:
        return "|".join((self.executable, self.model or "", self.profile or "", *self.extra_args))


def build_codex_command(config: CodexConfig) -> list[str]:
    command = [
        config.executable,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--color",
        "never",
        "--json",
    ]
    if config.model:
        command.extend(["--model", config.model])
    if config.profile:
        command.extend(["--profile", config.profile])
    command.extend(config.extra_args)
    command.append("-")
    return command


def _clean_output(output: str) -> str:
    value = output.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) >= 2:
            value = "\n".join(lines[1:-1]).strip()
    return value


def _extract_json_message(output: str) -> str | None:
    messages: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            messages.append(item["text"])
    return messages[-1] if messages else None


class CodexCliTranslator:
    """Run Codex CLI in non-interactive, read-only mode."""

    def __init__(
        self,
        config: CodexConfig | None = None,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.config = config or CodexConfig.from_env()
        self.runner = runner or subprocess.run
        self.last_stderr = ""

    def translate(self, prompt: str, *, cwd: Path | None = None) -> str:
        command = build_codex_command(self.config)
        run_cwd = cwd or self.config.workdir
        try:
            result = self.runner(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=str(run_cwd) if run_cwd else None,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TranslationError(f"Codex CLI 超时（{self.config.timeout_seconds:.0f} 秒）") from exc
        except OSError as exc:
            raise TranslationError(f"无法启动 Codex CLI：{exc}") from exc
        self.last_stderr = result.stderr or ""
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "未知错误").strip()
            raise TranslationError(f"Codex CLI 返回码 {result.returncode}：{detail[-2000:]}")
        output = _extract_json_message(result.stdout or "") or _clean_output(result.stdout or "")
        if not output:
            raise TranslationError("Codex CLI 返回了空翻译结果")
        return output
