import pytest
import subprocess
import re
import json
import threading
import time

from arxiv_latex_translator.latex import looks_untranslated, tokenize_latex, translate_latex, validate_protected_structure
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


def test_control_word_spacing_does_not_change_protected_structure() -> None:
    source = r"\large English 中文"
    translated, _ = translate_latex(source, FakeTranslator())
    validate_protected_structure(source, translated)


def test_emph_command_is_not_split_by_cjk_spacing() -> None:
    translated, _ = translate_latex(r"\emph{English} 中文", FakeTranslator())
    assert r"\emph{English}" in translated
    assert r"\em ph" not in translated


def test_comments_are_removed_and_title_stays_original() -> None:
    source = r"""\title{English Title} % remove this comment
Plain text % remove inline comment
\begin{comment}
Do not keep this block.
\end{comment}
\verb|% keep this literal|"""
    translated, _ = translate_latex(source, FakeTranslator())
    assert r"\title{English Title}" in translated
    assert "remove this comment" not in translated
    assert "Do not keep this block" not in translated
    assert r"\verb|% keep this literal|" in translated


def test_bibliography_environment_stays_original() -> None:
    source = r"""正文内容。
\begin{thebibliography}{1}
\bibitem{paper} An English bibliography entry that stays unchanged.
\end{thebibliography}"""
    translated, _ = translate_latex(source, FakeTranslator())
    assert "An English bibliography entry that stays unchanged." in translated


def test_percent_in_url_is_not_treated_as_a_comment() -> None:
    translated, _ = translate_latex(r"\url{https://example.com/a%20b} Text % comment", FakeTranslator())
    assert r"\url{https://example.com/a%20b}" in translated
    assert "comment" not in translated


def test_escaped_percent_is_preserved() -> None:
    translated, _ = translate_latex(r"50\% of text % comment", FakeTranslator())
    assert r"50\% of text" in translated
    assert "comment" not in translated


def test_marker_failure_is_reported_after_agent_retries() -> None:
    class BrokenTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            return "broken output"

    with pytest.raises(Exception, match="连续三次"):
        translate_latex(r"\section{Text} $x$", BrokenTranslator())


def test_failed_chunk_is_not_cached_as_a_translation() -> None:
    class BrokenTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            return "broken output"

    cache = {}
    with pytest.raises(Exception):
        translate_latex(r"\section{Text} $x$", BrokenTranslator(), chunk_cache=cache)
    assert cache == {}


def test_untranslated_english_chunk_is_retried() -> None:
    class EchoTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            if "<latex>\n" in prompt:
                return prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            return prompt.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]

    with pytest.raises(Exception, match="英文段落"):
        translate_latex("This is a sufficiently long English paragraph that must be translated.", EchoTranslator())


def test_plain_text_recovery_translates_a_chunk_after_marker_retries() -> None:
    class MarkerEchoTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0] if "<latex>" in prompt else prompt.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]
            return content if "<latex>" in prompt else content.replace("English paragraph", "中文段落")

    translated, _ = translate_latex("This is a sufficiently long English paragraph that must be translated. $x$", MarkerEchoTranslator())
    assert "中文段落" in translated


def test_untranslated_detector_ignores_short_technical_text() -> None:
    assert not looks_untranslated("GPT-6 Astra", "GPT-6 Astra")
    assert looks_untranslated(
        "This is a sufficiently long English paragraph that must be translated.",
        "This is a sufficiently long English paragraph that must be translated.",
    )


def test_cached_fallback_is_retried_after_translator_recovers() -> None:
    class RecoveringTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            return content.replace("Text", "译文")

    source = r"\section{Text} $x$"
    source_hash = __import__("hashlib").sha256(source.encode("utf-8")).hexdigest()
    cache = {"0": {"source_hash": source_hash, "translated": source, "fallback": "1"}}
    translated, stats = translate_latex(source, RecoveringTranslator(), chunk_cache=cache)
    assert translated == r"\section{译文} $x$"
    assert stats.fallbacks == 0


def test_marker_retry_uses_a_new_codex_call() -> None:
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        if len(commands) == 1:
            output = '{"type":"item.completed","item":{"type":"agent_message","text":"broken"}}\n'
        else:
            prompt = kwargs["input"]
            content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            output = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": content}}) + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    translator = CodexCliTranslator(CodexConfig(), runner=runner)
    translated, _ = translate_latex(r"Text $x$", translator)
    assert translated == r"Text $x$"
    assert commands[0][1] == "exec"
    assert commands[1][1] == "exec"


def test_codex_chunks_can_run_in_parallel() -> None:
    class ParallelTranslator:
        def __init__(self, tracker):
            self.tracker = tracker
            self.total_usage = type("Usage", (), {"add": lambda self, other: self})()

        def new_call(self):
            return ParallelTranslator(self.tracker)

        def translate(self, prompt: str, *, cwd=None) -> str:
            content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            with self.tracker["lock"]:
                self.tracker["active"] += 1
                self.tracker["max_active"] = max(self.tracker["max_active"], self.tracker["active"])
            time.sleep(0.03)
            with self.tracker["lock"]:
                self.tracker["active"] -= 1
            return content

    tracker = {"lock": threading.Lock(), "active": 0, "max_active": 0}
    source = " ".join(f"sentence-{index}" for index in range(40))
    translate_latex(source, ParallelTranslator(tracker), max_chars=30, max_parallel=3)
    assert tracker["max_active"] >= 2


def test_inline_verbatim_and_protected_structure_are_preserved() -> None:
    source = r"Text \verb|a_b| and \lstinline|x+y|."
    translated, _ = translate_latex(source, FakeTranslator())
    validate_protected_structure(source, translated)
    assert r"\verb|a_b|" in translated
    assert r"\lstinline|x+y|" in translated
