from pathlib import Path
import time

from arxiv_latex_translator.pipeline import Job, JobManager


def test_completed_job_paths_survive_restart_without_api_leak(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job_dir = tmp_path / "jobs" / "job-1"
    job = Job(id="job-1", source="2401.12345", arxiv_id="2401.12345", status="completed")
    job.pdf_path = str(job_dir / "artifacts" / "translated.pdf")
    job.source_path = str(job_dir / "artifacts" / "translated-source.zip")
    job.log_path = str(job_dir / "job.log")
    manager._jobs[job.id] = job
    manager._persist(job)

    reloaded = JobManager(tmp_path / "jobs").get("job-1")
    assert reloaded is not None
    assert reloaded.pdf_path == job.pdf_path
    assert "pdf_path" not in reloaded.to_dict()
    assert reloaded.to_dict()["pdf_url"] == "/api/jobs/job-1/pdf"
    assert reloaded.to_dict()["original_pdf_url"] == "https://arxiv.org/pdf/2401.12345"


def test_token_usage_fields_survive_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_MODEL", "gpt-6.2-sol")
    manager = JobManager(tmp_path / "jobs")
    job = Job(
        id="job-usage",
        source="2401.12345",
        arxiv_id="2401.12345",
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=30,
        reasoning_output_tokens=5,
        total_tokens=130,
    )
    manager._jobs[job.id] = job
    manager._persist(job)
    reloaded = JobManager(tmp_path / "jobs").get(job.id)
    assert reloaded is not None
    assert reloaded.total_tokens == 130
    assert reloaded.cached_input_tokens == 20
    payload = reloaded.to_dict()
    assert payload["uncached_input_tokens"] == 80
    assert payload["cost_usd"] == 0.000462
    assert payload["pricing_model"] == "gpt-6.2-sol"


def test_codex_auth_failure_is_exposed_as_disconnected(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = Job(
        id="job-auth",
        source="2401.12345",
        arxiv_id="2401.12345",
        status="failed",
        error="Codex CLI 返回码 1: invalid_refresh_token, please sign in again",
    )
    manager._jobs[job.id] = job
    manager._persist(job)
    reloaded = JobManager(tmp_path / "jobs")
    assert reloaded.codex_status()["status"] == "checking"


def test_codex_login_probe_reports_connected(tmp_path: Path, monkeypatch) -> None:
    manager = JobManager(tmp_path / "jobs")

    def fake_run(*args, **kwargs):
        return type("Result", (), {"returncode": 0, "stdout": "Logged in using ChatGPT", "stderr": ""})()

    monkeypatch.setattr("arxiv_latex_translator.pipeline.subprocess.run", fake_run)
    manager._codex_probe_at = 0
    manager.refresh_codex_status()
    for _ in range(20):
        if manager.codex_status()["status"] == "connected":
            break
        time.sleep(0.01)
    assert manager.codex_status()["status"] == "connected"


def test_completed_translation_is_reused_for_same_arxiv_id(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = Job(
        id="job-completed",
        source="https://arxiv.org/abs/2401.12345v1",
        arxiv_id="2401.12345v1",
        status="completed",
    )
    manager._jobs[job.id] = job
    reused = manager.create("2401.12345")
    assert reused is job


def test_history_groups_versions_and_prefers_completed_latest_version(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    older = Job(id="job-v1", source="2401.12345v1", arxiv_id="2401.12345v1", status="completed", updated_at=1)
    newer_failed = Job(id="job-v2", source="2401.12345v2", arxiv_id="2401.12345v2", status="failed", updated_at=2)
    manager._jobs[older.id] = older
    manager._jobs[newer_failed.id] = newer_failed
    history = manager.list()
    assert len(history) == 1
    assert history[0].arxiv_id == "2401.12345v1"
    assert history[0].to_dict()["display_arxiv_id"] == "2401.12345"


def test_retry_preserves_completed_translation_files(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = Job(
        id="job-retry",
        source="2401.12345",
        arxiv_id="2401.12345",
        status="failed",
        attempts=1,
        translated_files=["main.tex"],
    )
    manager._jobs[job.id] = job
    manager._persist(job)
    manager._run_with_limit = lambda job, resume=False: None
    retried = manager.retry(job.id)
    assert retried is job
    assert retried.attempts == 2
    assert retried.translated_files == ["main.tex"]
    assert retried.message == "准备复用已下载源码重试"


def test_retry_has_no_attempt_limit(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = Job(
        id="job-migration",
        source="2401.12345",
        arxiv_id="2401.12345",
        status="failed",
        attempts=3,
        error="翻译结果中的 LaTeX 保护结构发生变化",
    )
    manager._jobs[job.id] = job
    manager._run_with_limit = lambda job, resume=False: None
    retried = manager.retry(job.id)
    assert retried.attempts == 4


def test_compile_repair_runs_automatically_with_a_bounded_counter(tmp_path: Path, monkeypatch) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-compile", source="2401.12345", arxiv_id="2401.12345", status="running")
    manager._jobs[job.id] = job
    manager._run_compile_repair = lambda current: manager._update(
        current, compile_repair_attempts=current.compile_repair_attempts + 1
    )
    monkeypatch.setenv("ARXIV_MAX_COMPILE_REPAIRS", "2")

    assert manager._start_automatic_compile_repair(job) is True
    assert job.compile_repair_attempts == 1
    assert "自动修复" in job.message
    job.compile_repair_attempts = 2
    assert manager._start_automatic_compile_repair(job) is False


# --- End-to-end pipeline harness: fake arXiv download, fake Codex, fake LaTeX ---

import io
import json
import re
import sys
import tarfile

import arxiv_latex_translator.pipeline as pipeline_module
from arxiv_latex_translator.translator import CodexCliTranslator

ENGLISH = "This sentence is long English prose about neural networks and learning systems here."
APPENDIX_ENGLISH = "That appendix sentence explains extra derivations and proofs in great detail today."


def _install_harness(
    monkeypatch, tmp_path: Path, files: dict[str, str], *, latex_requires: str = "", quality_requires: str = ""
) -> None:
    def fake_download(identifier, dest, **kwargs):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content in files.items():
                data = content.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        dest.write_bytes(buffer.getvalue())
        return dest

    def fake_run(self, command, prompt, *, cwd):
        if "maintain a debugging playbook" in prompt:
            text = (
                "<lesson>\n### Missing macro after translation\n- Symptom: undefined citations\n"
                "- Cause: test\n- Fix: define the macro\n</lesson>"
            )
        elif "LaTeX build engineer" in prompt:
            preamble = cwd / "preamble.tex"
            preamble.write_text(preamble.read_text() + "\\newcommand{\\repaired}{1}\n")
            text = "repaired preamble"
        else:
            body = re.search(r"<latex>\n(.*)\n</latex>", prompt, re.S).group(1)
            text = re.sub(r"(?<![A-Za-z0-9<])(?<!</)(?!LATEXPROT\d{4}X)[A-Za-z](?:(?!LATEXPROT)[A-Za-z'-])*", "中", body)
        return "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}),
        ])

    script = tmp_path / "fake_latex.py"
    script.write_text(
        "import pathlib, sys\n"
        "name = sys.argv[-1]\n"
        f"if {latex_requires!r} and {latex_requires!r} not in pathlib.Path('preamble.tex').read_text():\n"
        "    print('LaTeX Error: missing macro'); sys.exit(1)\n"
        f"clean = not {quality_requires!r} or {quality_requires!r} in pathlib.Path('preamble.tex').read_text()\n"
        "log = 'Output written.\\n' if clean else ''.join(f\"Package natbib Warning: Citation `key{i}' on page 1 undefined on input line 3.\\n\" for i in range(5))\n"
        "pathlib.Path(name).with_suffix('.log').write_text(log)\n"
        "pathlib.Path(name).with_suffix('.pdf').write_bytes(b'%PDF-1.4')\n"
    )
    monkeypatch.setenv("ARXIV_REPAIR_SKILL_DIR", str(tmp_path / "skill"))
    monkeypatch.setattr(pipeline_module, "download_source", fake_download)
    monkeypatch.setattr(pipeline_module, "fetch_title", lambda identifier, **kwargs: "A Test Paper Title")
    monkeypatch.setattr(CodexCliTranslator, "_run", fake_run)
    monkeypatch.setenv("LATEX_COMMAND", f"{sys.executable} {script}")


def _single_file_paper() -> dict[str, str]:
    main = (
        "\\documentclass{article}\n\\input{preamble}\n\\begin{document}\n\\section{Intro}\n"
        f"{ENGLISH}\n\n\\appendix\n\\section{{Extra}}\n{APPENDIX_ENGLISH}\n\\end{{document}}\n"
    )
    return {"main.tex": main, "preamble.tex": "\\usepackage{amsmath}\n"}


def test_single_file_root_skips_appendix_until_requested(tmp_path: Path, monkeypatch) -> None:
    _install_harness(monkeypatch, tmp_path, _single_file_paper())
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-appendix", source="2401.12345", arxiv_id="2401.12345")
    manager._jobs[job.id] = job

    manager._run(job)
    translated = (tmp_path / "jobs" / job.id / "translated" / "main.tex").read_text()
    assert job.status == "completed", job.error
    assert job.appendix_files == ["main.tex"]
    assert job.title == "A Test Paper Title"
    assert ENGLISH not in translated
    assert APPENDIX_ENGLISH in translated
    assert "\\input{preamble}\n\\begin{document}\n\\section{Intro}" in translated

    job.translate_appendix = True
    manager._run(job, resume=True)
    translated = (tmp_path / "jobs" / job.id / "translated" / "main.tex").read_text()
    assert job.status == "completed", job.error
    assert APPENDIX_ENGLISH not in translated
    assert translated.count("\\appendix") == 1
    assert job.appendix_files == []


def test_compile_repair_edits_survive_recompile(tmp_path: Path, monkeypatch) -> None:
    _install_harness(monkeypatch, tmp_path, _single_file_paper(), latex_requires="\\repaired")
    monkeypatch.setenv("ARXIV_MAX_COMPILE_REPAIRS", "2")
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-repair", source="2401.12345", arxiv_id="2401.12345")
    manager._jobs[job.id] = job

    manager._run(job)
    assert job.status == "completed", job.error
    assert job.compile_repair_attempts == 1
    assert "\\repaired" in (tmp_path / "jobs" / job.id / "translated" / "preamble.tex").read_text()
    assert job.appendix_files == ["main.tex"]


def test_manual_compile_repair_gets_one_attempt_after_automatic_limit(tmp_path: Path, monkeypatch) -> None:
    _install_harness(monkeypatch, tmp_path, _single_file_paper(), latex_requires="\\repaired")
    monkeypatch.setenv("ARXIV_MAX_COMPILE_REPAIRS", "0")
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-manual", source="2401.12345", arxiv_id="2401.12345")
    manager._jobs[job.id] = job

    manager._run(job)
    assert job.status == "failed" and job.compile_failed

    manager.repair_compile(job.id)
    deadline = time.time() + 10
    while job.status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.05)
    assert job.status == "completed", job.error
    assert job.compile_repair_attempts == 1


def test_compile_repair_prompt_carries_previous_attempts_and_latest_log() -> None:
    log = (
        "translation noise\ncompile command: tectonic --keep-logs main.tex\nerror: old failure\n"
        "Codex compile repair:\nGuarded fontenc in style.cls\n--- retry: reusing downloaded source ---\n"
        "compile command: tectonic --keep-logs main.tex\nerror: style.cls:23: microtype Error\n"
    )
    prompt = JobManager._compile_repair_prompt("main.tex", log, compiler=JobManager._last_compile_command(log))
    assert "Attempt 1: Guarded fontenc in style.cls" in prompt
    assert "style.cls:23: microtype Error" in prompt
    assert "old failure" not in prompt.split("Latest compiler log:")[1]
    assert "../source" in prompt
    assert "tectonic --keep-logs main.tex" in prompt


def test_seed_skill_documents_known_fixes() -> None:
    from arxiv_latex_translator.repair_skill import SEED

    seed = SEED.read_text()
    assert seed.startswith("---\nname: arxiv-latex-repair\n")
    assert "\\DisableLigatures" in seed and "\\input{<main>.bbl}" in seed and "## Learned cases" in seed


def test_abstract_defined_in_root_preamble_is_translated(tmp_path: Path, monkeypatch) -> None:
    abstract = "Our abstract describes a discrete visual tokenizer that unifies many different vision tasks."
    main = (
        "\\documentclass{article}\n\\newcommand{\\keepme}{Keep this macro text}\n"
        f"\\abstract{{\n{abstract}\n\\par\\href{{https://example.com}}{{Project Page}}\n}}\n"
        f"\\begin{{document}}\n\\maketitle\n{ENGLISH}\n\\end{{document}}\n"
    )
    _install_harness(monkeypatch, tmp_path, {"main.tex": main, "preamble.tex": ""})
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-abstract", source="2401.12345", arxiv_id="2401.12345")
    manager._jobs[job.id] = job

    manager._run(job)
    translated = (tmp_path / "jobs" / job.id / "translated" / "main.tex").read_text()
    assert job.status == "completed", job.error
    assert abstract not in translated
    assert "\\abstract{" in translated and "\\href{https://example.com}" in translated
    assert "\\newcommand{\\keepme}{Keep this macro text}" in translated
    assert ENGLISH not in translated


def test_history_search_matches_paper_title(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-title", source="2611.12426", arxiv_id="2611.12426", status="completed", title="dNaViT: Discrete Codes")
    other = Job(id="job-other", source="2401.00001", arxiv_id="2401.00001", status="completed", title="Something else")
    for item in (job, other):
        manager._jobs[item.id] = item
        manager._persist(item)
    assert [found.id for found in manager.list("discrete codes")] == ["job-title"]
    assert JobManager(tmp_path / "jobs").get("job-title").to_dict()["title"] == "dNaViT: Discrete Codes"


def test_title_backfill_keeps_history_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_module, "fetch_title", lambda identifier, **kwargs: "Backfilled Title")
    monkeypatch.setattr(pipeline_module.time, "sleep", lambda seconds: None)
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-old", source="2401.00001", arxiv_id="2401.00001", status="completed")
    manager._jobs[job.id] = job
    manager._persist(job)
    job.updated_at = 1000.0
    manager._persist(job, touch=False)
    manager.start_title_backfill()
    deadline = time.time() + 5
    while not job.title and time.time() < deadline:
        time.sleep(0.02)
    assert job.title == "Backfilled Title"
    assert JobManager(tmp_path / "jobs").get("job-old").updated_at == 1000.0


def test_run_time_accumulates_across_runs_and_excludes_queueing(tmp_path: Path, monkeypatch) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(pipeline_module.time, "time", lambda: clock["now"])
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-time", source="2401.00001", arxiv_id="2401.00001")
    manager._jobs[job.id] = job

    def fake_run(current, resume=False):
        assert current.to_dict()["elapsed_seconds"] == 0 if not resume else 60
        clock["now"] += 60

    manager._run = fake_run
    manager._run_with_limit(job)
    assert job.elapsed_seconds == 60 and job.started_at is None
    manager._run_with_limit(job, resume=True)
    assert job.elapsed_seconds == 120

    job.started_at = clock["now"]
    clock["now"] += 30
    assert job.to_dict()["elapsed_seconds"] == 150


def test_interrupted_run_time_is_closed_on_restart_and_single_runs_are_backfilled(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    interrupted = Job(id="job-cut", source="2401.00001", arxiv_id="2401.00001", status="running", elapsed_seconds=10, started_at=100.0)
    interrupted.updated_at = 160.0
    old = Job(id="job-old", source="2401.00002", arxiv_id="2401.00002", status="completed", attempts=1, created_at=500.0)
    old.updated_at = 800.0
    retried = Job(id="job-retried", source="2401.00003", arxiv_id="2401.00003", status="completed", attempts=4, created_at=500.0)
    retried.updated_at = 9000.0
    for job in (interrupted, old, retried):
        manager._persist(job, touch=False)

    reloaded = JobManager(tmp_path / "jobs")
    assert reloaded.get("job-cut").elapsed_seconds == 70 and reloaded.get("job-cut").started_at is None
    assert reloaded.get("job-old").elapsed_seconds == 300
    assert reloaded.get("job-retried").elapsed_seconds == 0


def test_clean_exit_with_undefined_citations_triggers_repair_and_records_a_lesson(tmp_path: Path, monkeypatch) -> None:
    from arxiv_latex_translator.repair_skill import skill_file

    _install_harness(monkeypatch, tmp_path, _single_file_paper(), quality_requires="\\repaired")
    monkeypatch.setenv("ARXIV_MAX_COMPILE_REPAIRS", "2")
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-quality", source="2401.12345", arxiv_id="2401.12345")
    manager._jobs[job.id] = job

    manager._run(job)
    assert job.status == "completed", job.error
    assert job.compile_repair_attempts == 1
    assert job.quality_issues == []
    skill = skill_file(tmp_path / "jobs").read_text()
    assert "### Missing macro after translation" in skill and "job-quality" in skill
    log = (tmp_path / "jobs" / job.id / "job.log").read_text()
    assert "build check: undefined_citations" in log


def test_unresolved_quality_issues_complete_with_visible_warnings(tmp_path: Path, monkeypatch) -> None:
    _install_harness(monkeypatch, tmp_path, _single_file_paper(), quality_requires="\\never")
    monkeypatch.setenv("ARXIV_MAX_COMPILE_REPAIRS", "1")
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-warn", source="2401.12345", arxiv_id="2401.12345")
    manager._jobs[job.id] = job

    manager._run(job)
    assert job.status == "completed", job.error
    assert job.compile_repair_attempts == 1
    assert job.quality_issues and "citation keys are undefined" in job.quality_issues[0]


def test_repair_prompt_includes_findings_and_the_skill(tmp_path: Path) -> None:
    prompt = JobManager._compile_repair_prompt(
        "main.tex", "compile command: tectonic main.tex\n", compiler="tectonic main.tex",
        findings=["120 citation keys are undefined"], playbook="### Bibliography database missing",
    )
    assert "120 citation keys are undefined" in prompt
    assert "### Bibliography database missing" in prompt


def test_recheck_rebuilds_a_completed_job_and_repairs_quality_issues(tmp_path: Path, monkeypatch) -> None:
    _install_harness(monkeypatch, tmp_path, _single_file_paper(), quality_requires="\\repaired")
    monkeypatch.setenv("ARXIV_MAX_COMPILE_REPAIRS", "0")
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-recheck", source="2401.12345", arxiv_id="2401.12345")
    manager._jobs[job.id] = job
    manager._run(job)
    assert job.status == "completed" and job.quality_issues

    monkeypatch.setenv("ARXIV_MAX_COMPILE_REPAIRS", "1")
    manager.recheck(job.id)
    deadline = time.time() + 10
    while job.status in {"queued", "running"} and time.time() < deadline:
        time.sleep(0.05)
    assert job.status == "completed", job.error
    assert job.quality_issues == []
    assert job.compile_repair_attempts == 1
