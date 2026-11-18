import pytest
import subprocess
import re
import json

from arxiv_latex_translator.latex import tokenize_latex, translate_latex, validate_protected_structure
from arxiv_latex_translator.translator import CodexCliTranslator, CodexConfig


class FakeTranslator:
    def translate(self, prompt: str, *, cwd=None) -> str:
        content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
        return (
            content.replace("Introduction", "介绍")
            .replace("A figure", "一个图")
            .replace("Plain text", "普通文本")
            .replace("Short caption", "短标题")
            .replace("Long caption", "长标题")
        )


def test_table_body_math_and_references_are_protected() -> None:
    source = r"""\section{Introduction}
Plain text $x^2$ \cite{paper}.
\caption{A figure}
\begin{tabular}{cc} A & B \\ C & D \end{tabular}
"""
    translated, stats = translate_latex(source, FakeTranslator())
    assert "\\section{介绍}" in translated
    assert "普通文本" in translated
    assert "\\caption{一个图}" in translated
    assert "$x^2$" in translated
    assert "\\cite{paper}" in translated
    assert "A & B" in translated
    assert stats.chunks >= 1


def test_tokenizer_keeps_macro_definition_as_one_protected_region() -> None:
    tokens = tokenize_latex(r"\newcommand{\foo}[1]{#1 + 1}\section{Text}")
    assert tokens[0].kind == "protected"
    assert "\\newcommand" in tokens[0].value


def test_href_url_stays_protected_and_optional_caption_text_is_translatable() -> None:
    source = r"\caption[Short caption]{Long caption} \href{https://example.com}{Plain text}"
    translated, _ = translate_latex(source, FakeTranslator())
    assert "\\caption[短标题]{长标题}" in translated
    assert "\\href{https://example.com}{普通文本}" in translated


def test_marker_order_changes_are_rebound_to_original_protected_tokens() -> None:
    class ReorderingTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            markers = __import__("re").findall(r"LATEXPROT\d{4}X", content)
            if len(markers) >= 2:
                content = content.replace(markers[0], "TEMP", 1).replace(markers[1], markers[0], 1).replace("TEMP", markers[1], 1)
            return content

    translated, _ = translate_latex(r"\section{Text} $x$", ReorderingTranslator())
    assert translated == r"\section{Text} $x$"


def test_control_word_is_separated_from_adjacent_chinese_text() -> None:
    translated, _ = translate_latex(r"\large English", FakeTranslator())
    assert translated.startswith(r"\large ")


def test_emph_command_is_not_split_by_cjk_spacing() -> None:
    translated, _ = translate_latex(r"\emph{English} 中文", FakeTranslator())
    assert r"\emph{English}" in translated
    assert r"\em ph" not in translated


def test_marker_failure_falls_back_to_original_chunk() -> None:
    class BrokenTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            return "broken output"

    translated, stats = translate_latex(r"\section{Text} $x$", BrokenTranslator())
    assert translated == r"\section{Text} $x$"
    assert stats.fallbacks == 1


def test_marker_retry_resumes_the_codex_thread() -> None:
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            output = '{"type":"thread.started","thread_id":"thread-1"}\n{"type":"item.completed","item":{"type":"agent_message","text":"broken"}}\n'
        else:
            prompt = kwargs["input"]
            content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            output = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": content}}) + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    translator = CodexCliTranslator(CodexConfig(), runner=runner)
    translated, _ = translate_latex(r"Text $x$", translator)
    assert translated == r"Text $x$"
    assert commands[1][2] == "resume"
    assert "thread-1" in commands[1]


def test_inline_verbatim_and_protected_structure_are_preserved() -> None:
    source = r"Text \verb|a_b| and \lstinline|x+y|."
    translated, _ = translate_latex(source, FakeTranslator())
    validate_protected_structure(source, translated)
    assert r"\verb|a_b|" in translated
    assert r"\lstinline|x+y|" in translated
