from pathlib import Path

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
    assert reloaded.codex_status()["status"] == "unknown"


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
