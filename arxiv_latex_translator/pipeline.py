"""Job orchestration for the arXiv translation workflow."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .arxiv import ArxivIdentifier, download_source, extract_source, find_main_tex, parse_arxiv_input
from .compiler import compile_latex
from .latex import translate_latex, validate_protected_structure
from .translator import CodexCliTranslator, TokenUsage, TranslationError


@dataclass
class Job:
    id: str
    source: str
    arxiv_id: str
    status: str = "queued"
    phase: str = "queued"
    progress: int = 0
    message: str = "等待开始"
    error: str | None = None
    pdf_path: str | None = None
    source_path: str | None = None
    log_path: str | None = None
    attempts: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    translated_files: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("pdf_path", None)
        value.pop("source_path", None)
        value.pop("log_path", None)
        value["arxiv_url"] = f"https://arxiv.org/abs/{self.arxiv_id}"
        value["original_pdf_url"] = f"https://arxiv.org/pdf/{self.arxiv_id}"
        if self.pdf_path:
            value["pdf_url"] = f"/api/jobs/{self.id}/pdf"
        if self.source_path:
            value["source_url"] = f"/api/jobs/{self.id}/source"
        if self.log_path:
            value["log_url"] = f"/api/jobs/{self.id}/log"
        return value


class JobManager:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(os.environ.get("ARXIV_OUTPUT_DIR", "data/jobs"))
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._codex_status = {
            "status": "unknown",
            "message": "等待首次 Codex CLI 调用",
            "checked_at": None,
        }
        self._semaphore = threading.Semaphore(int(os.environ.get("ARXIV_MAX_CONCURRENT_JOBS", "1")))
        self._load_jobs()

    def _job_dir(self, job_id: str) -> Path:
        return self.base_dir / job_id

    def _job_json(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _load_jobs(self) -> None:
        for path in self.base_dir.glob("*/job.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**{key: data[key] for key in Job.__dataclass_fields__ if key in data})
                base = self.base_dir.resolve()
                for path_value in (job.pdf_path, job.source_path, job.log_path):
                    if path_value and base not in Path(path_value).resolve().parents:
                        raise ValueError("任务记录包含工作目录之外的路径")
                if job.status in {"queued", "running"}:
                    job.status = "failed"
                    job.phase = "failed"
                    job.message = "服务重启导致任务中断"
                    job.error = "服务重启后任务未能恢复，请重试"
                self._jobs[job.id] = job
                if job.status == "failed":
                    self._persist(job)
            except (OSError, ValueError, TypeError):
                continue
        for job in self._jobs.values():
            if job.error and self._is_codex_auth_error(job.error):
                self._codex_status = {
                    "status": "unknown",
                    "message": "历史任务曾出现认证错误，等待新的 Codex CLI 调用验证",
                    "checked_at": job.updated_at,
                }
                break

    @staticmethod
    def _is_codex_auth_error(message: str) -> bool:
        lowered = message.casefold()
        return any(
            marker in lowered
            for marker in (
                "invalid_refresh_token",
                "invalid_jwt",
                "not logged in",
                "sign in again",
                "重新登录",
            )
        )

    def _set_codex_status(self, status: str, message: str) -> None:
        with self._lock:
            self._codex_status = {
                "status": status,
                "message": message,
                "checked_at": time.time(),
            }

    def codex_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._codex_status)

    def _persist(self, job: Job) -> None:
        job.updated_at = time.time()
        path = self._job_json(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _job_id(self, identifier: ArxivIdentifier) -> str:
        config_key = "|".join(
            os.environ.get(name, "")
            for name in (
                "CODEX_EXECUTABLE",
                "CODEX_MODEL",
                "CODEX_PROFILE",
                "CODEX_EXTRA_ARGS",
                "CODEX_CHUNK_CHARS",
                "LATEX_COMMAND",
                "LATEX_TIMEOUT_SECONDS",
            )
        )
        digest = hashlib.sha256(f"{identifier.value}|{config_key}".encode()).hexdigest()
        return f"{identifier.base_id.replace('/', '-')}-{digest[:10]}"

    def _reusable_job(self, identifier: ArxivIdentifier) -> Job | None:
        explicit_version = identifier.value != identifier.base_id
        with self._lock:
            candidates = [job for job in self._jobs.values() if job.status == "completed"]
        candidates.sort(key=lambda job: job.updated_at, reverse=True)
        for job in candidates:
            if job.arxiv_id == identifier.value:
                return job
            if not explicit_version:
                try:
                    existing = parse_arxiv_input(job.arxiv_id)
                except ValueError:
                    continue
                if existing.base_id == identifier.base_id:
                    return job
        return None

    def create(self, source: str, *, force: bool = False) -> Job:
        identifier = parse_arxiv_input(source)
        if not force:
            reusable = self._reusable_job(identifier)
            if reusable is not None:
                return reusable
        job_id = self._job_id(identifier)
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing and existing.status in {"queued", "running"}:
                return existing
            if existing and existing.status == "completed" and not force:
                return existing
            if existing and existing.status == "failed" and not force:
                return existing
            if existing and existing.attempts >= 3:
                raise RuntimeError("该任务已达到三次尝试上限")
            job = Job(id=job_id, source=source, arxiv_id=identifier.value)
            job.attempts = (existing.attempts if existing else 0) + 1
            self._jobs[job.id] = job
            self._persist(job)
            thread = threading.Thread(target=self._run_with_limit, args=(job, False), daemon=True)
            thread.start()
            return job

    def retry(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "failed":
                raise RuntimeError("只有失败任务可以重试")
            if job.attempts >= 3:
                raise RuntimeError("该任务已达到三次尝试上限")
            job.status = "queued"
            job.phase = "queued"
            job.progress = min(job.progress, 89)
            job.message = "准备复用已下载源码重试"
            job.error = None
            job.attempts += 1
            self._persist(job)
            thread = threading.Thread(target=self._run_with_limit, args=(job, True), daemon=True)
            thread.start()
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, query: str = "", limit: int = 100) -> list[Job]:
        needle = query.strip().casefold()
        with self._lock:
            jobs = list(self._jobs.values())
        if needle:
            jobs = [
                job
                for job in jobs
                if needle in job.arxiv_id.casefold()
                or needle in job.source.casefold()
                or needle in job.id.casefold()
            ]
        jobs.sort(key=lambda job: job.updated_at, reverse=True)
        return jobs[: max(limit, 0)]

    def _update(self, job: Job, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)
            self._persist(job)

    def _log(self, log_path: Path, message: str) -> None:
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(message.rstrip() + "\n")

    def _run_with_limit(self, job: Job, resume: bool = False) -> None:
        with self._semaphore:
            self._run(job, resume=resume)

    def _run(self, job: Job, *, resume: bool = False) -> None:
        job_dir = self._job_dir(job.id)
        log_path = job_dir / "job.log"
        source_archive = job_dir / "source.archive"
        extracted_dir = job_dir / "source"
        translated_dir = job_dir / "translated"
        artifact_dir = job_dir / "artifacts"
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
            can_resume = resume and source_archive.is_file() and extracted_dir.is_dir() and translated_dir.is_dir()
            if not can_resume:
                for path in (source_archive, extracted_dir, translated_dir, artifact_dir):
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                log_path.write_text("", encoding="utf-8")
                self._update(job, translated_files=[], status="running", phase="download", progress=10, message="正在下载 LaTeX 源码", log_path=str(log_path))
            else:
                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write("\n--- retry: reusing downloaded source and completed files ---\n")
                self._update(job, status="running", phase="translate", progress=max(job.progress, 25), message="正在复用源码并继续未完成翻译", log_path=str(log_path))
            identifier = parse_arxiv_input(job.source)
            if not can_resume:
                download_source(
                    identifier,
                    source_archive,
                    user_agent=os.environ.get("ARXIV_USER_AGENT", "arxiv-latex-translator/0.1"),
                )
                self._log(log_path, f"downloaded {identifier.value} to {source_archive}")
                extract_source(source_archive, extracted_dir)
            root = find_main_tex(extracted_dir)
            self._log(log_path, f"main tex: {root.relative_to(extracted_dir)}")

            self._update(job, phase="translate", progress=25, message="正在保护 LaTeX 结构并调用 Codex CLI")
            self._set_codex_status("checking", "正在调用 Codex CLI")
            if translated_dir.exists() and not can_resume:
                shutil.rmtree(translated_dir)
            if not translated_dir.exists():
                shutil.copytree(extracted_dir, translated_dir)
            translated_root = translated_dir / root.relative_to(extracted_dir)
            translator = CodexCliTranslator()
            translator.total_usage = TokenUsage(
                input_tokens=job.input_tokens,
                cached_input_tokens=job.cached_input_tokens,
                output_tokens=job.output_tokens,
                reasoning_output_tokens=job.reasoning_output_tokens,
            )
            chunk_chars = int(os.environ.get("CODEX_CHUNK_CHARS", "2600"))
            tex_files = sorted(translated_dir.rglob("*.tex"))
            total = max(len(tex_files), 1)
            translated_files = set(job.translated_files)
            total_chunks = 0
            total_characters = 0
            for index, tex_file in enumerate(tex_files, start=1):
                relative_name = str(tex_file.relative_to(translated_dir))
                if relative_name in translated_files:
                    continue
                original = tex_file.read_text(encoding="utf-8", errors="replace")
                translated, stats = translate_latex(original, translator, max_chars=chunk_chars, cwd=job_dir)
                self._set_codex_status("connected", "最近一次 Codex CLI 调用成功")
                tex_file.write_text(translated, encoding="utf-8")
                validate_protected_structure(original, translated)
                translated_files.add(relative_name)
                total_chunks += stats.chunks
                total_characters += stats.characters
                usage = translator.total_usage
                self._update(
                    job,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_output_tokens=usage.reasoning_output_tokens,
                    total_tokens=usage.total_tokens,
                    translated_files=sorted(translated_files),
                )
                progress = 25 + int(index / total * 40)
                self._update(job, progress=progress, message=f"已翻译 {index}/{total} 个 TeX 文件")
            usage = translator.total_usage
            self._log(
                log_path,
                "translated files={} chunks={} characters={} input_tokens={} cached_input_tokens={} output_tokens={} reasoning_output_tokens={} total_tokens={}".format(
                    len(tex_files),
                    total_chunks,
                    total_characters,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    usage.total_tokens,
                ),
            )

            self._update(job, phase="compile", progress=70, message="正在编译中文 PDF")
            result = compile_latex(translated_root, artifact_dir)
            self._log(log_path, f"compile command: {' '.join(result.command)}")
            self._log(log_path, f"compile returncode: {result.returncode}")
            self._log(log_path, result.stdout)
            self._log(log_path, result.stderr)
            if result.returncode != 0 or result.pdf_path is None:
                raise RuntimeError("LaTeX 编译失败，请查看编译日志")

            self._update(job, phase="package", progress=90, message="正在打包翻译后的源码")
            source_zip = artifact_dir / "translated-source.zip"
            generated_suffixes = {".aux", ".bbl", ".blg", ".fls", ".fdb_latexmk", ".log", ".out", ".toc", ".xdv"}
            with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(translated_dir.rglob("*")):
                    is_root_pdf = path == translated_root.with_suffix(".pdf")
                    if path.is_file() and path.suffix not in generated_suffixes and not path.name.endswith(".synctex.gz") and not is_root_pdf:
                        archive.write(path, path.relative_to(translated_dir))
            self._update(
                job,
                status="completed",
                phase="completed",
                progress=100,
                message="翻译和编译已完成",
                pdf_path=str(result.pdf_path),
                source_path=str(source_zip),
            )
        except Exception as exc:
            self._log(log_path, f"ERROR: {exc}")
            if isinstance(exc, TranslationError):
                detail = str(exc)
                status = "disconnected" if self._is_codex_auth_error(detail) else "error"
                message = "Codex CLI 认证失效，请重新登录" if status == "disconnected" else detail[-500:]
                self._set_codex_status(status, message)
            self._update(job, status="failed", phase="failed", message="任务失败", error=str(exc), log_path=str(log_path))

