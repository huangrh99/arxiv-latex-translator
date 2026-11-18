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


def test_token_usage_fields_survive_restart(tmp_path: Path) -> None:
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


def test_codex_session_id_stays_internal(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    job = Job(id="job-session", source="2401.12345", arxiv_id="2401.12345", codex_session_id="thread-secret")
    assert "codex_session_id" not in job.to_dict()


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


def test_protection_migration_retry_is_only_available_once(tmp_path: Path) -> None:
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
    assert retried.attempts == 1
    assert retried.migration_retry_used is True
    retried.status = "failed"
    retried.attempts = 3
    retried.error = "翻译结果中的 LaTeX 保护结构发生变化"
    try:
        manager.retry(job.id)
    except RuntimeError as exc:
        assert "三次" in str(exc)
    else:
        raise AssertionError("migration retry should not reset the limit twice")
