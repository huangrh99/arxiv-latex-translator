import subprocess
from pathlib import Path

from arxiv_latex_translator.compiler import _ensure_cjk_support, _missing_packages, _write_package_shims, compile_latex, compiler_command
from arxiv_latex_translator.translator import CodexConfig, CodexCliTranslator, build_codex_command, build_codex_repair_command


def test_codex_command_is_non_interactive_and_read_only() -> None:
    command = build_codex_command(CodexConfig(model="gpt-test", profile="local"))
    assert command[:2] == ["codex", "exec"]
    assert "--sandbox" in command
    assert "read-only" in command
    assert "model_reasoning_effort=\"medium\"" in command
    assert command[-1] == "-"


def test_codex_repair_command_is_workspace_scoped() -> None:
    command = build_codex_repair_command(CodexConfig(model="gpt-test"))
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--ephemeral" in command
    assert command[-1] == "-"


def test_codex_adapter_uses_injected_runner() -> None:
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            '{"type":"item.completed","item":{"type":"agent_message","text":"translated"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":4,"output_tokens":7,"reasoning_output_tokens":2}}\n',
            "",
        )

    translator = CodexCliTranslator(CodexConfig(), runner=runner)
    assert translator.translate("prompt") == "translated"
    assert seen["input"] == "prompt"
    assert translator.last_usage.input_tokens == 12
    assert translator.last_usage.cached_input_tokens == 4
    assert translator.last_usage.output_tokens == 7
    assert translator.total_usage.total_tokens == 19


def test_compiler_command_disables_shell_escape(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LATEX_COMMAND", "latexmk -xelatex")
    command = compiler_command(tmp_path / "main.tex")
    assert "-no-shell-escape" in command
    assert "-f" not in command


def test_compiler_command_uses_tectonic_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LATEX_COMMAND", raising=False)
    monkeypatch.setattr(
        "arxiv_latex_translator.compiler.shutil.which",
        lambda name: "/opt/homebrew/bin/tectonic" if name == "tectonic" else None,
    )
    command = compiler_command(tmp_path / "main.tex")
    assert command == ["tectonic", "--keep-logs", "main.tex"]


def test_cjk_support_is_injected_for_plain_latex(tmp_path: Path) -> None:
    root = tmp_path / "main.tex"
    root.write_text("\\documentclass{article}\n\\begin{document}\n中文\n\\end{document}\n", encoding="utf-8")
    _ensure_cjk_support(root)
    result = root.read_text(encoding="utf-8")
    assert "\\usepackage{xeCJK}" in result
    assert "\\setCJKmainfont{Songti SC}" in result


def test_cjk_microtype_document_compiles(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LATEX_COMMAND", "xelatex")
    root = tmp_path / "main.tex"
    root.write_text(
        "\\documentclass{article}\n\\usepackage{microtype}\n\\begin{document}\n中文\n\\end{document}\n",
        encoding="utf-8",
    )
    result = compile_latex(root, tmp_path / "artifacts")
    assert result.returncode == 0
    assert result.pdf_path is not None


def test_known_missing_package_gets_a_local_shim(tmp_path: Path) -> None:
    assert _missing_packages("! LaTeX Error: File `simpleicons.sty' not found.") == ["simpleicons"]
    created = _write_package_shims(tmp_path / "main.tex", "! LaTeX Error: File `simpleicons.sty' not found.")
    assert created == ["simpleicons"]
    assert "ProvidesPackage{simpleicons}" in (tmp_path / "simpleicons.sty").read_text(encoding="utf-8")
