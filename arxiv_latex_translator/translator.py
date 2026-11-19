"""Codex CLI translation adapter."""

from __future__ import annotations

import os
import json
import shlex
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence


class TranslationError(RuntimeError):
    """Raised when the configured translation command fails."""


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=self.reasoning_output_tokens + other.reasoning_output_tokens,
        )


@dataclass(frozen=True)
class CodexConfig:
    executable: str = "codex"
    model: str | None = None
    fallback_model: str | None = None
    profile: str | None = None
    workdir: Path | None = None
    extra_args: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    reasoning_effort: str = "medium"

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
            fallback_model=os.environ.get("CODEX_FALLBACK_MODEL") or None,
            profile=os.environ.get("CODEX_PROFILE") or None,
            workdir=Path(workdir_value).expanduser() if workdir_value else None,
            extra_args=extra,
            timeout_seconds=timeout,
            reasoning_effort=os.environ.get("CODEX_REASONING_EFFORT", "medium"),
        )

    def cache_key(self) -> str:
        return "|".join((self.executable, self.model or "", self.fallback_model or "", self.profile or "", self.reasoning_effort, *self.extra_args))


def build_codex_command(config: CodexConfig) -> list[str]:
    command = [
        config.executable,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--json",
        "-c",
        f'model_reasoning_effort="{config.reasoning_effort}"',
    ]
    if config.model:
        command.extend(["--model", config.model])
    if config.profile:
        command.extend(["--profile", config.profile])
    command.extend(config.extra_args)
    command.append("-")
    return command


def build_codex_repair_command(config: CodexConfig) -> list[str]:
    command = [
        config.executable,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--ephemeral",
        "--color",
        "never",
        "--json",
        "-c",
        f'model_reasoning_effort="{config.reasoning_effort}"',
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
    message, _ = _extract_json_result(output)
    return message


def _extract_json_result(output: str) -> tuple[str | None, TokenUsage]:
    messages: list[str] = []
    usage = TokenUsage()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            if event.get("type") == "turn.completed":
                raw_usage = event.get("usage") or {}
                usage = TokenUsage(
                    input_tokens=int(raw_usage.get("input_tokens", 0) or 0),
                    cached_input_tokens=int(raw_usage.get("cached_input_tokens", 0) or 0),
                    output_tokens=int(raw_usage.get("output_tokens", 0) or 0),
                    reasoning_output_tokens=int(raw_usage.get("reasoning_output_tokens", 0) or 0),
                )
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            messages.append(item["text"])
    return (messages[-1] if messages else None), usage


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
        self.last_usage = TokenUsage()
        self.total_usage = TokenUsage()
        self._fallback_used = False

    def _run(self, command: list[str], prompt: str, *, cwd: Path | None) -> str:
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
        return result.stdout or ""

    def translate(self, prompt: str, *, cwd: Path | None = None) -> str:
        command = build_codex_command(self.config)
        output_text = self._run(command, prompt, cwd=cwd)
        output, usage = _extract_json_result(output_text)
        self.last_usage = usage
        self.total_usage = self.total_usage.add(usage)
        output = output or _clean_output(output_text)
        if not output:
            raise TranslationError("Codex CLI 返回了空翻译结果")
        return output

    def new_call(self) -> "CodexCliTranslator":
        """Create an independent translator for a parallel chunk call."""

        return CodexCliTranslator(self.config, runner=self.runner)

    def use_fallback_model(self) -> bool:
        if self._fallback_used or not self.config.fallback_model:
            return False
        self.config = replace(self.config, model=self.config.fallback_model)
        self._fallback_used = True
        return True

    def repair(self, prompt: str, *, cwd: Path | None = None) -> str:
        output_text = self._run(build_codex_repair_command(self.config), prompt, cwd=cwd)
        output, usage = _extract_json_result(output_text)
        self.last_usage = usage
        self.total_usage = self.total_usage.add(usage)
        return output or _clean_output(output_text)
