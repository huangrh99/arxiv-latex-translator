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
