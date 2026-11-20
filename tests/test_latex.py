import pytest
import subprocess
import re
import json
import threading
import time

from arxiv_latex_translator.latex import _make_chunks, looks_untranslated, tokenize_latex, translate_latex, validate_protected_structure
from arxiv_latex_translator.translator import CodexCliTranslator, CodexConfig, TranslationError


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


def test_chunks_accumulate_complete_lines_to_target() -> None:
    source = "a" * 600 + "\n" + "b" * 600 + "\n" + "c" * 600 + "\n"
    chunks = _make_chunks(tokenize_latex(source), 1000)
    assert len(chunks) == 2
    assert chunks[0][0].endswith("\n")
    assert len(chunks[0][0]) >= 1000
    assert "\n" in chunks[1][0]


def test_tokenizer_keeps_macro_definition_as_one_protected_region() -> None:
    tokens = tokenize_latex(r"\newcommand{\foo}[1]{#1 + 1}\section{Text}")
    assert tokens[0].kind == "protected"
    assert "\\newcommand" in tokens[0].value


def test_cross_reference_commands_protect_targets() -> None:
    source = r"See \Cref{fig:transfer} and \cref{sec:intro}."
    protected = [token.value for token in tokenize_latex(source) if token.kind == "protected"]
    assert protected == [r"\Cref{fig:transfer}", r"\cref{sec:intro}"]


def test_href_url_stays_protected_and_optional_caption_text_is_translatable() -> None:
    source = r"\caption[Short caption]{Long caption} \href{https://example.com}{Plain text}"
    translated, _ = translate_latex(source, FakeTranslator())
    assert "\\caption[短标题]{长标题}" in translated
    assert "\\href{https://example.com}{普通文本}" in translated


def test_marker_order_changes_are_rebound_to_original_protected_tokens() -> None:
    class ReorderingTranslator:
        def __init__(self):
            self.calls = 0

        def translate(self, prompt: str, *, cwd=None) -> str:
            content = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            markers = __import__("re").findall(r"LATEXPROT\d{4}X", content)
            self.calls += 1
            if self.calls == 1 and len(markers) >= 2:
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
    assert all("translated" not in entry for entry in cache.values())
    assert any("last_error" in entry for entry in cache.values())


def test_resume_sends_previous_failure_reason_in_first_prompt() -> None:
    class BrokenTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            return "broken output"

    prompts: list[str] = []

    class RecordingTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            prompts.append(prompt)
            return "broken output"

    source = r"\section{Text} with enough English words to need a real translation here $x$"
    cache = {}
    with pytest.raises(Exception):
        translate_latex(source, BrokenTranslator(), chunk_cache=cache)
    with pytest.raises(Exception):
        translate_latex(source, RecordingTranslator(), chunk_cache=cache)
    assert "A previous run of this chunk failed validation" in prompts[0]
    assert "sentence is missing from the response" in prompts[0]


def test_prompt_explains_markers_and_retry_names_the_bad_marker() -> None:
    prompts: list[str] = []

    class SwappingTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            prompts.append(prompt)
            # Drop the citation marker every time.
            body = prompt.rsplit("<latex>\n", 1)[1].split("\n</latex>", 1)[0]
            return re.sub(r"LATEXPROT\d{4}X(?=\s*\.)", "", body).replace("shows", "显示")

    with pytest.raises(Exception):
        translate_latex(r"See \Cref{fig:a} which shows the results~\citep{smith}.", SwappingTranslator())
    assert "cross-reference" in prompts[0]
    assert "citation" in prompts[0]
    assert "sentence 1: missing LATEXPROT" in prompts[1]


def test_out_of_order_references_are_not_silently_swapped() -> None:
    from arxiv_latex_translator.latex import ProtectedRegionError, _replace_markers

    protected = {"LATEXPROT0001X": r"\Cref{fig:a}", "LATEXPROT0002X": r"\Cref{fig:b}"}
    with pytest.raises(ProtectedRegionError):
        _replace_markers("先看 LATEXPROT0002X 再看 LATEXPROT0001X", protected)
    braces = {"LATEXPROT0001X": "}", "LATEXPROT0002X": "}"}
    assert _replace_markers("a LATEXPROT0002X b LATEXPROT0001X", braces) == "a } b }"


def test_untranslated_english_chunk_is_retried() -> None:
    class EchoTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            if "<latex>\n" in prompt:
                return prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            return prompt.split("<text>\n", 1)[1].rsplit("\n</text>", 1)[0]

    with pytest.raises(Exception, match="left untranslated"):
        translate_latex("This is a sufficiently long English paragraph that must be translated.", EchoTranslator())


def test_only_failed_sentences_are_sent_again() -> None:
    prompts: list[str] = []

    class HalfTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            prompts.append(prompt)
            body = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            if len(prompts) == 1:
                # First answer translates sentence 1 but leaves sentence 2 in English.
                return body.replace("The first sentence explains the method in great detail here.", "第一句详细解释了方法。")
            return body.replace("The second sentence reports the experimental results across all benchmarks.", "第二句报告了所有基准上的实验结果。")

    source = (
        "The first sentence explains the method in great detail here. "
        "The second sentence reports the experimental results across all benchmarks."
    )
    translated, _ = translate_latex(source, HalfTranslator())
    assert translated == "第一句详细解释了方法。 第二句报告了所有基准上的实验结果。"
    assert "<s 1>" not in prompts[1] and "<s 2>" in prompts[1]
    assert "sentence 2: English text was left untranslated" in prompts[1]


def test_standalone_markers_may_move_inside_a_sentence_but_pairs_may_not() -> None:
    from arxiv_latex_translator.latex import check_sentence

    protected = {"LATEXPROT0001X": "\\%", "LATEXPROT0002X": "\\emph{", "LATEXPROT0003X": "}"}
    source = "It solves 30LATEXPROT0001X of LATEXPROT0002XStandardLATEXPROT0003X tests overall."
    assert check_sentence(source, "在 LATEXPROT0002X标准LATEXPROT0003X 测试中共解决 30LATEXPROT0001X。", protected) is None
    assert "paired" in check_sentence(source, "30LATEXPROT0001X LATEXPROT0003X标准LATEXPROT0002X 测试。", protected)
    assert "LaTeX syntax" in check_sentence(source, "解决 30% LATEXPROT0001X 的 LATEXPROT0002X标准LATEXPROT0003X 测试。", protected)


def test_validator_ignores_commands_that_become_adjacent() -> None:
    from arxiv_latex_translator.latex import validate_protected_structure

    validate_protected_structure("61\\% of the \\emph{Standard} runs", "61\\% \\emph{标准} 运行")


def test_untranslated_detector_ignores_short_technical_text() -> None:
    assert not looks_untranslated("GPT-6 Astra", "GPT-6 Astra")
    assert looks_untranslated(
        "This is a sufficiently long English paragraph that must be translated.",
        "This is a sufficiently long English paragraph that must be translated.",
    )
    assert looks_untranslated(
        "中文开头。 This is a sufficiently long English paragraph that must be translated.",
        "中文开头。 This is a sufficiently long English paragraph that must be translated.",
    )


def test_transient_codex_capacity_error_is_retried() -> None:
    class FlakyTranslator:
        def __init__(self):
            self.calls = 0

        def translate(self, prompt: str, *, cwd=None) -> str:
            self.calls += 1
            if self.calls == 1:
                raise TranslationError("Codex CLI 返回码 1: Selected model is at capacity")
            return prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0].replace("English", "中文")

    translator = FlakyTranslator()
    translated, _ = translate_latex("English prose that is long enough to translate.", translator)
    assert "中文" in translated
    assert translator.calls == 2


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


LAYOUT_FIGURE = r"""\begin{figure}[t]
  \centering
  \setlength{\parskip}{0pt}
  \captionsetup[sub]{skip=0pt,justification=centering}
  \includegraphics[width=\linewidth]{figures/plot.pdf}\par
  \nointerlineskip\vspace{-9pt}
  \rlap{\makebox[\linewidth][c]{\smash{\raisebox{-23.8pt}{\includegraphics{figures/legend.pdf}}}}}
  \begin{minipage}[t]{0.4621\linewidth}
    \subcaption{VQA}
  \end{minipage}
  \caption{Scaling curves show that the encoder free model keeps improving with more data.}
\end{figure}
"""


def test_layout_arguments_are_not_treated_as_untranslated_prose() -> None:
    class ProseOnlyTranslator:
        def translate(self, prompt: str, *, cwd=None) -> str:
            body = prompt.split("<latex>\n", 1)[1].rsplit("\n</latex>", 1)[0]
            # Translate the caption sentence only, as a careful translator would.
            return body.replace(
                "Scaling curves show that the encoder free model keeps improving with more data.",
                "缩放曲线表明，无编码器模型随着数据增多持续提升。",
            )

    translated, _ = translate_latex(LAYOUT_FIGURE, ProseOnlyTranslator())
    assert "skip=0pt,justification=centering" in translated
    assert "缩放曲线表明" in translated


def test_single_word_headings_still_need_translation() -> None:
    from arxiv_latex_translator.latex import _needs_translation

    assert _needs_translation("LATEXPROT0001XIntroductionLATEXPROT0002X")
    assert not _needs_translation("LATEXPROT0001XtLATEXPROT0002X\n  LATEXPROT0003Xskip=0pt,justification=centeringLATEXPROT0004X 0pt")
