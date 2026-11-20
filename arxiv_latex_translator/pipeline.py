"""Job orchestration for the arXiv translation workflow."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .arxiv import fetch_title, ArxivIdentifier, download_source, extract_source, find_main_tex, parse_arxiv_input
from .compiler import compile_latex
from .build_check import inspect_build
from .latex import ProtectedRegionError, _read_balanced, looks_untranslated, strip_latex_comments, tokenize_latex, translate_latex
from .pricing import configured_pricing_model, token_cost
from .repair_skill import append_lesson, lesson_prompt, parse_lesson, skill_for_prompt
from .translator import CodexCliTranslator, CodexConfig, TokenUsage, TranslationError


TRANSLATION_CACHE_VERSION = "latex-protection-v3"


@dataclass
class Job:
    id: str
    source: str
    arxiv_id: str
    title: str = ""
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
    compile_failed: bool = False
    compile_repair_attempts: int = 0
    # Build-check findings for the latest successful compile (empty when the PDF is clean).
    quality_issues: list[str] = field(default_factory=list)
    repairs_to_learn: bool = False
    translated_files: list[str] = field(default_factory=list)
    appendix_files: list[str] = field(default_factory=list)
    translate_appendix: bool = False
    config_key: str = ""
    pricing_model: str = ""
    # Active run time in seconds, summed over every run of this job (queueing excluded).
    elapsed_seconds: float = 0.0
    started_at: float | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("pdf_path", None)
        value.pop("source_path", None)
        value.pop("log_path", None)
        running = max(0.0, time.time() - self.started_at) if self.started_at else 0.0
        value["elapsed_seconds"] = round(self.elapsed_seconds + running)
        value["arxiv_url"] = f"https://arxiv.org/abs/{self.arxiv_id}"
        value["original_pdf_url"] = f"https://arxiv.org/pdf/{self.arxiv_id}"
        try:
            value["display_arxiv_id"] = parse_arxiv_input(self.arxiv_id).base_id
        except ValueError:
            value["display_arxiv_id"] = self.arxiv_id
        if self.pdf_path:
            value["pdf_url"] = f"/api/jobs/{self.id}/pdf"
        if self.source_path:
            value["source_url"] = f"/api/jobs/{self.id}/source"
        if self.log_path:
            value["log_url"] = f"/api/jobs/{self.id}/log"
        value.update(
            token_cost(
                self.input_tokens,
                self.cached_input_tokens,
                self.output_tokens,
                model=self.pricing_model or configured_pricing_model(),
            )
        )
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
        self._codex_probe_at = 0.0
        self._codex_probe_lock = threading.Lock()
        self._semaphore = threading.Semaphore(int(os.environ.get("ARXIV_MAX_CONCURRENT_JOBS", "1")))
        self._load_jobs()

    def start_title_backfill(self) -> None:
        """Fill in missing paper titles in the background, one arXiv API request every 3 seconds."""

        def backfill() -> None:
            with self._lock:
                missing = [job for job in self._jobs.values() if not job.title]
            titles: dict[str, str] = {}
            for job in missing:
                try:
                    identifier = parse_arxiv_input(job.arxiv_id)
                except ValueError:
                    continue
                if identifier.base_id not in titles:
                    titles[identifier.base_id] = fetch_title(
                        identifier, user_agent=os.environ.get("ARXIV_USER_AGENT", "arxiv-latex-translator/0.1")
                    )
                    time.sleep(3)
                if titles[identifier.base_id] and not job.title:
                    with self._lock:
                        job.title = titles[identifier.base_id]
                        self._persist(job, touch=False)

        threading.Thread(target=backfill, daemon=True).start()

    def _job_dir(self, job_id: str) -> Path:
        path = (self.base_dir / job_id).resolve()
        if self.base_dir.resolve() not in path.parents:
            raise ValueError("任务 ID 无效")
        return path

    def _job_json(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _load_jobs(self) -> None:
        for path in self.base_dir.glob("*/job.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**{key: data[key] for key in Job.__dataclass_fields__ if key in data})
                if not re.fullmatch(r"[A-Za-z0-9._-]+", job.id):
                    raise ValueError("任务 ID 无效")
                base = self.base_dir.resolve()
                for path_value in (job.pdf_path, job.source_path, job.log_path):
                    if path_value and base not in Path(path_value).resolve().parents:
                        raise ValueError("任务记录包含工作目录之外的路径")
                if not job.pricing_model:
                    job.pricing_model = configured_pricing_model()
                timing_changed = False
                if job.started_at:
                    # The service stopped mid-run; count the run up to its last recorded update.
                    job.elapsed_seconds += max(0.0, job.updated_at - job.started_at)
                    job.started_at = None
                    timing_changed = True
                if not job.elapsed_seconds and job.status == "completed" and job.attempts <= 1 and job.updated_at > job.created_at:
                    # Jobs from before run timing existed: a single run spans creation to completion.
                    job.elapsed_seconds = job.updated_at - job.created_at
                    timing_changed = True
                if job.status in {"queued", "running"}:
                    job.status = "failed"
                    job.phase = "failed"
                    job.message = "服务重启导致任务中断"
                    job.error = "服务重启后任务未能恢复，请重试"
                self._jobs[job.id] = job
                if job.status == "failed" or timing_changed:
                    self._persist(job, touch=False)
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

    def _probe_codex_login(self) -> None:
        try:
            config = CodexConfig.from_env()
            result = subprocess.run(
                [config.executable, "login", "status"],
                capture_output=True,
                text=True,
                timeout=min(config.timeout_seconds, 10),
                check=False,
            )
            detail = (result.stdout or result.stderr or "").strip()
            if result.returncode == 0:
                self._set_codex_status("connected", "Codex CLI 已登录，可开始翻译")
            elif self._is_codex_auth_error(detail):
                self._set_codex_status("disconnected", "Codex CLI 未登录或认证已失效")
            else:
                self._set_codex_status("error", detail[-300:] or "Codex CLI 登录状态检查失败")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self._set_codex_status("error", f"Codex CLI 状态检查失败：{exc}")

    def refresh_codex_status(self) -> None:
        now = time.time()
        with self._codex_probe_lock:
            if now - self._codex_probe_at < 30:
                return
            with self._lock:
                if self._codex_status["status"] == "checking":
                    return
                self._codex_status = {
                    "status": "checking",
                    "message": "正在检查 Codex CLI 登录状态",
                    "checked_at": now,
                }
            self._codex_probe_at = now
        threading.Thread(target=self._probe_codex_login, daemon=True).start()

    def codex_status(self) -> dict[str, Any]:
        self.refresh_codex_status()
        with self._lock:
            return dict(self._codex_status)

    def _persist(self, job: Job, *, touch: bool = True) -> None:
        # Metadata backfills (touch=False) must not change the time shown and sorted in history.
        if touch:
            job.updated_at = time.time()
        path = self._job_json(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _job_id(self, identifier: ArxivIdentifier) -> str:
        digest = hashlib.sha256(f"{identifier.value}|{self._config_key()}".encode()).hexdigest()
        return f"{identifier.base_id.replace('/', '-')}-{digest[:10]}"

    @staticmethod
    def _config_key() -> str:
        return "|".join(
            os.environ.get(name, "")
            for name in (
                "CODEX_EXECUTABLE",
                "CODEX_MODEL",
                "CODEX_FALLBACK_MODEL",
                "CODEX_PROFILE",
                "CODEX_EXTRA_ARGS",
                "CODEX_CHUNK_CHARS",
                "CODEX_REASONING_EFFORT",
                "LATEX_COMMAND",
                "LATEX_TIMEOUT_SECONDS",
                "ARXIV_MAX_COMPILE_REPAIRS",
            )
        )

    def _reusable_job(self, identifier: ArxivIdentifier) -> Job | None:
        explicit_version = identifier.value != identifier.base_id
        with self._lock:
            candidates = [job for job in self._jobs.values() if job.status == "completed"]
        candidates.sort(key=lambda job: job.updated_at, reverse=True)
        for job in candidates:
            if job.config_key and job.config_key != self._config_key():
                continue
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
            job = Job(
                id=job_id,
                source=source,
                arxiv_id=identifier.value,
                config_key=self._config_key(),
                pricing_model=configured_pricing_model(),
            )
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
            job.status = "queued"
            job.phase = "queued"
            job.progress = min(job.progress, 89)
            job.message = "准备复用已下载源码重试"
            job.error = None
            job.compile_failed = False
            job.compile_repair_attempts = 0
            job.pdf_path = None
            job.source_path = None
            job.attempts += 1
            self._persist(job)
            thread = threading.Thread(target=self._run_with_limit, args=(job, True), daemon=True)
            thread.start()
            return job

    def recheck(self, job_id: str) -> Job:
        """Rebuild a completed job's existing translation, run the build check, and repair it."""

        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "completed":
                raise RuntimeError("只有已完成任务可以重新检查编译")
            job_dir = self._job_dir(job.id)
            if not (job_dir / "translated").is_dir() or not (job_dir / "source").is_dir():
                raise RuntimeError("翻译源码目录不存在，无法重新检查编译")
            job.status = "queued"
            job.phase = "compile"
            job.progress = 70
            job.message = "等待重新编译并检查 PDF"
            job.error = None
            job.compile_repair_attempts = 0
            self._persist(job)
            thread = threading.Thread(target=self._run_recheck_with_limit, args=(job,), daemon=True)
            thread.start()
            return job

    def _run_recheck_with_limit(self, job: Job) -> None:
        with self._semaphore:
            self._timed(job, lambda: self._run_recheck(job))

    def _run_recheck(self, job: Job) -> None:
        job_dir = self._job_dir(job.id)
        log_path = job_dir / "job.log"
        try:
            self._log(log_path, "\n--- recheck: rebuilding the existing translation ---")
            self._update(job, status="running")
            source_dir = job_dir / "source"
            root = job_dir / "translated" / find_main_tex(source_dir).relative_to(source_dir)
            self._compile_and_finish(job, root)
        except Exception as exc:
            self._log(log_path, f"ERROR: recheck failed: {exc}")
            self._update(job, status="failed", phase="failed", message="任务失败", error=str(exc), log_path=str(log_path))

    def repair_compile(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "failed" or job.phase != "failed" or not job.compile_failed:
                raise RuntimeError("只有编译失败任务可以让 Codex 修复编译")
            translated_dir = self._job_dir(job.id) / "translated"
            if not translated_dir.is_dir():
                raise RuntimeError("翻译源码目录不存在，无法让 Codex 修复编译")
            job.status = "queued"
            job.phase = "compile"
            job.progress = 70
            job.message = "等待 Codex 修复 LaTeX 编译"
            job.error = None
            job.pdf_path = None
            job.source_path = None
            self._persist(job)
            thread = threading.Thread(target=self._run_compile_repair_with_limit, args=(job,), daemon=True)
            thread.start()
            return job

    def translate_appendix(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status != "completed":
                raise RuntimeError("只有已完成任务可以继续翻译 Appendix")
            if not job.appendix_files:
                raise RuntimeError("该任务没有待翻译的 Appendix")
            job.status = "queued"
            job.phase = "queued"
            job.progress = 70
            job.message = "准备翻译 Appendix"
            job.error = None
            job.pdf_path = None
            job.source_path = None
            job.translate_appendix = True
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
            filtered: list[Job] = []
            for job in jobs:
                try:
                    base_id = parse_arxiv_input(job.arxiv_id).base_id.casefold()
                except ValueError:
                    base_id = ""
                if (
                    needle in base_id
                    or needle in job.arxiv_id.casefold()
                    or needle in job.source.casefold()
                    or needle in job.id.casefold()
                    or needle in job.title.casefold()
                ):
                    filtered.append(job)
            jobs = filtered
        grouped: dict[str, Job] = {}
        for job in jobs:
            try:
                identifier = parse_arxiv_input(job.arxiv_id)
                key = identifier.base_id
                version_match = re.search(r"v(\d+)$", identifier.value)
                version = int(version_match.group(1)) if version_match else 0
            except ValueError:
                key = job.arxiv_id
                version = 0
            current = grouped.get(key)
            if current is None:
                grouped[key] = job
                continue
            current_version_match = re.search(r"v(\d+)$", current.arxiv_id)
            current_version = int(current_version_match.group(1)) if current_version_match else 0
            current_rank = (current.status == "completed", current_version, current.updated_at)
            candidate_rank = (job.status == "completed", version, job.updated_at)
            if candidate_rank > current_rank:
                grouped[key] = job
        result = list(grouped.values())
        result.sort(key=lambda job: job.updated_at, reverse=True)
        return result[: max(limit, 0)]

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
            self._timed(job, lambda: self._run(job, resume=resume))

    def _run_compile_repair_with_limit(self, job: Job) -> None:
        with self._semaphore:
            self._timed(job, lambda: self._run_compile_repair(job, manual=True))

    def _timed(self, job: Job, run) -> None:
        """Add this run's wall time to the job; started after the queue so waiting is not counted."""

        with self._lock:
            job.started_at = time.time()
            self._persist(job, touch=False)
        try:
            run()
        finally:
            with self._lock:
                if job.started_at:
                    job.elapsed_seconds += max(0.0, time.time() - job.started_at)
                    job.started_at = None
                    self._persist(job, touch=False)

    @staticmethod
    def _compile_repair_limit() -> int:
        try:
            value = int(os.environ.get("ARXIV_MAX_COMPILE_REPAIRS", "3"))
        except ValueError as exc:
            raise ValueError("ARXIV_MAX_COMPILE_REPAIRS 必须是整数") from exc
        return max(0, value)

    def _start_automatic_compile_repair(self, job: Job, *, quality: bool = False) -> bool:
        limit = self._compile_repair_limit()
        if job.compile_repair_attempts >= limit:
            return False
        attempt = job.compile_repair_attempts + 1
        what = "编译质量问题" if quality else "LaTeX 编译"
        self._update(
            job,
            status="running",
            phase="compile",
            progress=72,
            message=f"正在自动修复{what}（{attempt}/{limit}）",
            error=None,
            compile_failed=True,
        )
        self._run_compile_repair(job)
        return True

    @staticmethod
    def _last_compile_command(log_text: str) -> str:
        commands = re.findall(r"^(?:repair )?compile command: (.+)$", log_text, re.M)
        return commands[-1] if commands else "XeLaTeX"

    @staticmethod
    def _repair_history(log_text: str) -> list[str]:
        return [
            summary.strip()
            for summary in re.findall(
                r"Codex compile repair:\n(.*?)(?=\n(?:repair )?compile command:|\n--- retry|\nERROR:|\nbuild check|\Z)",
                log_text,
                re.S,
            )
        ]

    @staticmethod
    def _compile_repair_prompt(
        root_name: str, log_text: str, *, compiler: str, findings: list[str] | None = None, playbook: str = ""
    ) -> str:
        """Instructions for Codex compile repair: findings, the learned skill, and the latest log."""

        start = max(log_text.rfind("compile command:"), 0)
        latest_log = log_text[start:][-15000:]
        history = "\n\n".join(
            f"Attempt {index}: {summary[:1500]}" for index, summary in enumerate(JobManager._repair_history(log_text), start=1)
        )
        problem = (
            "The build exits with code 0, but the build check found problems in the PDF. Fix their cause:\n"
            + "\n".join(f"- {finding}" for finding in findings)
            if findings
            else "The build fails. Fix the first fatal error in the log and any others with the same cause."
        )
        return f"""Role: you are a LaTeX build engineer. You know TeX Live and Tectonic, the XeTeX engine,
CJK typesetting with xeCJK, document classes and style files, and the pdfTeX-only features that
break under XeTeX. You fix build problems with minimal, well-reasoned edits and never change the
paper's content.

Task: an English arXiv paper was translated into Chinese. Repair the work tree in the current
directory so that `{root_name}` builds cleanly with `{compiler}` (XeTeX engine; the pipeline adds
xeCJK and a Chinese font automatically).

{problem}

Follow the repair playbook below. It holds the workflow, the known problems with their correct
fixes, and cases learned from earlier papers; prefer a matching case.

Rules:
- Do not translate, rewrite, or delete prose, formulas, citations, labels, tables, figures, or
  sections. Do not touch files outside this directory except reading `../source`.
- Do not repeat a fix listed under previous attempts; if it did not work, choose a different fix.
- Finish with a summary that lists every file you changed and why.

<playbook>
{playbook or "(unavailable)"}
</playbook>

Previous repair attempts for this job:
{history or "(none)"}

Latest compiler log:
{latest_log}
"""

    def _run_compile_repair(self, job: Job, *, manual: bool = False) -> None:
        job_dir = self._job_dir(job.id)
        log_path = job_dir / "job.log"
        translated_dir = job_dir / "translated"
        try:
            limit = self._compile_repair_limit()
            # A manual request is one extra attempt beyond the automatic limit.
            if not manual and job.compile_repair_attempts >= limit:
                raise RuntimeError("自动编译修复已达到上限")
            self._update(job, compile_repair_attempts=job.compile_repair_attempts + 1, repairs_to_learn=True)
            # Locate the root from the untouched source so a repair cannot change which file compiles.
            source_dir = job_dir / "source"
            base_dir = source_dir if source_dir.is_dir() else translated_dir
            root = translated_dir / find_main_tex(base_dir).relative_to(base_dir)
            self._update(job, status="running", phase="compile", progress=70, message="Codex 正在修复 LaTeX 编译")
            log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
            root_name = str(root.relative_to(translated_dir))
            prompt = self._compile_repair_prompt(
                root_name,
                log_text,
                compiler=self._last_compile_command(log_text),
                findings=job.quality_issues,
                playbook=skill_for_prompt(self.base_dir),
            )
            translator = CodexCliTranslator()
            repair_output = translator.repair(prompt, cwd=translated_dir)
            self._log(log_path, "Codex compile repair:")
            self._log(log_path, repair_output or "Codex 未返回修复摘要")
            usage = translator.total_usage
            self._update(
                job,
                input_tokens=job.input_tokens + usage.input_tokens,
                cached_input_tokens=job.cached_input_tokens + usage.cached_input_tokens,
                output_tokens=job.output_tokens + usage.output_tokens,
                reasoning_output_tokens=job.reasoning_output_tokens + usage.reasoning_output_tokens,
                total_tokens=job.total_tokens + usage.total_tokens,
            )
            self._compile_and_finish(job, root)
        except Exception as exc:
            self._log(log_path, f"ERROR: Codex compile repair failed: {exc}")
            self._update(job, status="failed", phase="failed", message="任务失败", error=str(exc), log_path=str(log_path))

    @staticmethod
    def _preamble_abstract_span(preamble: str) -> tuple[int, int] | None:
        """Locate the argument of a top-level \\abstract{...} call in the preamble."""

        for match in re.finditer(r"\\abstract\s*\{", preamble):
            before = preamble[max(0, match.start() - 16) : match.start()]
            if re.search(r"\\(?:re)?newcommand\*?\{?\s*$|\\(?:g|e|x)?def\s*$|\\providecommand\*?\{?\s*$", before):
                continue
            opening = match.end() - 1
            closing = _read_balanced(preamble, opening)
            if closing <= opening + 1 or preamble[closing - 1] != "}":
                return None
            return opening + 1, closing - 1
        return None

    @staticmethod
    def _is_appendix_file(relative_name: str) -> bool:
        parts = [part.casefold() for part in Path(relative_name).parts]
        return any("appendix" in part for part in parts)

    @staticmethod
    def _appendix_marker(text: str) -> re.Match[str] | None:
        masked: list[str] = []
        for token in tokenize_latex(text):
            value = token.value
            if token.kind == "protected" and re.match(r"\\begin\{(?:verbatim|verbatim\*|lstlisting|minted|comment)\}", value):
                value = re.sub(r"[^\n]", " ", value)
            masked.append(value)
        return re.search(r"(?m)^\s*\\appendix\b", "".join(masked))

    def _translate_table_captions(
        self,
        text: str,
        translator: CodexCliTranslator,
        *,
        max_chars: int,
        max_parallel: int,
        cwd: Path,
        log_path: Path,
        relative_name: str,
    ) -> tuple[str, int, int]:
        result: list[str] = []
        cursor = 0
        chunks = 0
        characters = 0
        pattern = re.compile(r"\\caption(?:\[[^]]*\])?\s*\{")
        for match in pattern.finditer(text):
            body_start = match.end() - 1
            body_end = _read_balanced(text, body_start)
            if body_end <= body_start or text[body_start] != "{":
                continue
            result.append(text[cursor : body_start + 1])
            original_caption = text[body_start + 1 : body_end - 1]
            try:
                translated_caption, stats = translate_latex(
                    original_caption,
                    translator,
                    max_chars=max_chars,
                    max_parallel=max_parallel,
                    cwd=cwd,
                )
                result.append(translated_caption)
                chunks += stats.chunks
                characters += stats.characters
            except Exception as exc:
                self._log(log_path, f"table caption preserved: {relative_name}: {exc}")
                result.append(original_caption)
            cursor = body_end - 1
        result.append(text[cursor:])
        return "".join(result), chunks, characters

    def _run(self, job: Job, *, resume: bool = False) -> None:
        job_dir = self._job_dir(job.id)
        log_path = job_dir / "job.log"
        source_archive = job_dir / "source.archive"
        extracted_dir = job_dir / "source"
        translated_dir = job_dir / "translated"
        artifact_dir = job_dir / "artifacts"
        cache_path = job_dir / "translation-cache.json"
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
            if not job.title:
                title = fetch_title(identifier, user_agent=os.environ.get("ARXIV_USER_AGENT", "arxiv-latex-translator/0.1"))
                if title:
                    self._update(job, title=title)
            if not can_resume:
                download_source(
                    identifier,
                    source_archive,
                    user_agent=os.environ.get("ARXIV_USER_AGENT", "arxiv-latex-translator/0.1"),
                )
                self._log(log_path, f"downloaded {identifier.value} to {source_archive}")
                extract_source(source_archive, extracted_dir)
            self._strip_comments_in_tree(extracted_dir)
            root = find_main_tex(extracted_dir)
            self._log(log_path, f"main tex: {root.relative_to(extracted_dir)}")

            self._update(job, phase="translate", progress=25, message="正在保护 LaTeX 结构并调用 Codex CLI")
            self._set_codex_status("checking", "正在调用 Codex CLI")
            if translated_dir.exists() and not can_resume:
                shutil.rmtree(translated_dir)
            if not translated_dir.exists():
                shutil.copytree(extracted_dir, translated_dir)
            self._strip_comments_in_tree(translated_dir)
            translated_root = translated_dir / root.relative_to(extracted_dir)
            translator = CodexCliTranslator()
            translator.total_usage = TokenUsage(
                input_tokens=job.input_tokens,
                cached_input_tokens=job.cached_input_tokens,
                output_tokens=job.output_tokens,
                reasoning_output_tokens=job.reasoning_output_tokens,
            )
            chunk_chars = int(os.environ.get("CODEX_CHUNK_CHARS", "1000"))
            chunk_parallel = int(os.environ.get("CODEX_MAX_PARALLEL_CHUNKS", "3"))
            if chunk_parallel <= 0:
                raise ValueError("CODEX_MAX_PARALLEL_CHUNKS 必须大于 0")
            tex_files = sorted(translated_dir.rglob("*.tex"))
            total = max(len(tex_files), 1)
            translated_files = set(job.translated_files)
            appendix_files = set(job.appendix_files)
            translation_cache: dict[str, dict[str, dict[str, str]]] = {}
            if can_resume and cache_path.is_file():
                try:
                    loaded_cache = json.loads(cache_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_cache, dict) and loaded_cache.get("_version") == TRANSLATION_CACHE_VERSION:
                        loaded_cache.pop("_version", None)
                        translation_cache = loaded_cache
                except (OSError, ValueError):
                    translation_cache = {}

            total_chunks = 0
            total_characters = 0
            total_fallbacks = 0
            for index, tex_file in enumerate(tex_files, start=1):
                relative_name = str(tex_file.relative_to(translated_dir))
                appendix_file = self._is_appendix_file(relative_name)
                source_tex_file = extracted_dir / tex_file.relative_to(translated_dir)
                if appendix_file and not job.translate_appendix:
                    original_appendix = source_tex_file.read_text(encoding="utf-8", errors="replace")
                    if tex_file.read_text(encoding="utf-8", errors="replace") != original_appendix:
                        tex_file.write_text(original_appendix, encoding="utf-8")
                    appendix_files.add(relative_name)
                    continue
                original = source_tex_file.read_text(encoding="utf-8", errors="replace")
                if tex_file.name.casefold() == "preamble.tex" and relative_name in translated_files:
                    continue
                if tex_file.name.casefold() == "preamble.tex":
                    # Preamble files contain package setup and macro definitions.
                    # They are structural LaTeX, not paper prose, and must stay byte-for-byte unchanged.
                    tex_file.write_text(original, encoding="utf-8")
                    translated_files.add(relative_name)
                    self._update(job, translated_files=sorted(translated_files), appendix_files=sorted(appendix_files))
                    continue
                if relative_name in translated_files:
                    translated_existing = tex_file.read_text(encoding="utf-8", errors="replace")
                    if not looks_untranslated(original, translated_existing):
                        continue
                    translated_files.remove(relative_name)
                if "tables" in {part.casefold() for part in Path(relative_name).parts}:
                    translated, table_chunks, table_characters = self._translate_table_captions(
                        original,
                        translator,
                        max_chars=chunk_chars,
                        max_parallel=chunk_parallel,
                        cwd=job_dir,
                        log_path=log_path,
                        relative_name=relative_name,
                    )
                    tex_file.write_text(translated, encoding="utf-8")
                    translated_files.add(relative_name)
                    total_chunks += table_chunks
                    total_characters += table_characters
                    self._update(
                        job,
                        translated_files=sorted(translated_files),
                        appendix_files=sorted(appendix_files),
                        input_tokens=translator.total_usage.input_tokens,
                        cached_input_tokens=translator.total_usage.cached_input_tokens,
                        output_tokens=translator.total_usage.output_tokens,
                        reasoning_output_tokens=translator.total_usage.reasoning_output_tokens,
                        total_tokens=translator.total_usage.total_tokens,
                    )
                    progress = 25 + int(index / total * 40)
                    self._update(job, progress=progress, message=f"已翻译 {index}/{total} 个 TeX 文件")
                    continue
                marker = self._appendix_marker(original)
                is_root = relative_name == str(root.relative_to(extracted_dir)) and "\\begin{document}" in original
                # The root file is mostly a preamble containing package options, macro
                # definitions, and the title. Only its document body is sent to Codex so
                # those control structures cannot be corrupted.
                body_start = original.find("\\begin{document}") if is_root else 0
                main_cache_key = f"{relative_name}::body" if is_root else relative_name

                def translate_segment(segment: str, cache_key: str):
                    return translate_latex(
                        segment,
                        translator,
                        max_chars=chunk_chars,
                        max_parallel=chunk_parallel,
                        cwd=job_dir,
                        chunk_cache=translation_cache.setdefault(cache_key, {}),
                        on_chunk_cached=lambda _key, _entry: self._persist_chunk_progress(job, translator, translation_cache, cache_path),
                    )

                # Some templates define the abstract in the preamble with \abstract{...}. The rest of
                # the preamble stays untouched; only that argument is translated.
                prefix = original[:body_start]
                abstract_span = self._preamble_abstract_span(prefix) if is_root else None
                if abstract_span is not None:
                    abstract_start, abstract_end = abstract_span
                    abstract_source = prefix[abstract_start:abstract_end]
                    translated_abstract, abstract_stats = translate_segment(abstract_source, f"{relative_name}::abstract")
                    if looks_untranslated(abstract_source, translated_abstract):
                        raise ProtectedRegionError("摘要翻译结果仍包含未翻译的英文段落")
                    prefix = prefix[:abstract_start] + translated_abstract + prefix[abstract_end:]
                    total_chunks += abstract_stats.chunks
                    total_characters += abstract_stats.characters

                if marker and not job.translate_appendix:
                    # Translate the main text only; keep everything after \appendix in English
                    # until the explicit Appendix action runs.
                    main_source = original[body_start : marker.end()]
                    translated_main, stats = translate_segment(main_source, main_cache_key)
                    translated = prefix + translated_main + original[marker.end() :]
                    validation_source = main_source
                    validation_result = translated_main
                    appendix_files.add(relative_name)
                elif marker and job.translate_appendix and not appendix_file:
                    working = tex_file.read_text(encoding="utf-8", errors="replace")
                    working_marker = self._appendix_marker(working)
                    if working_marker is not None:
                        head = working[: working_marker.end()]
                    else:
                        # The working copy lost its \appendix line; rebuild the main text from the chunk cache.
                        translated_main, _ = translate_segment(original[body_start : marker.end()], main_cache_key)
                        head = prefix + translated_main
                    appendix_source = original[marker.end() :]
                    translated_appendix, stats = translate_segment(appendix_source, f"{relative_name}::appendix")
                    translated = head + translated_appendix
                    validation_source = appendix_source
                    validation_result = translated_appendix
                else:
                    body_source = original[body_start:]
                    translated_body, stats = translate_segment(body_source, main_cache_key)
                    translated = prefix + translated_body
                    validation_source = body_source
                    validation_result = translated_body
                self._set_codex_status("connected", "最近一次 Codex CLI 调用成功")
                if not appendix_file and looks_untranslated(validation_source, validation_result):
                    error = "翻译结果仍包含未翻译的英文段落，已阻止该文件进入编译"
                    self._log(log_path, f"translation-completeness validation failed: {relative_name}: {error}")
                    raise ProtectedRegionError(error)
                tex_file.write_text(translated, encoding="utf-8")
                if not marker or job.translate_appendix:
                    translated_files.add(relative_name)
                total_chunks += stats.chunks
                total_characters += stats.characters
                total_fallbacks += stats.fallbacks
                usage = translator.total_usage
                self._update(
                    job,
                    input_tokens=usage.input_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    output_tokens=usage.output_tokens,
                    reasoning_output_tokens=usage.reasoning_output_tokens,
                    total_tokens=usage.total_tokens,
                    translated_files=sorted(translated_files),
                    appendix_files=sorted(appendix_files),
                )
                progress = 25 + int(index / total * 40)
                self._update(job, progress=progress, message=f"已翻译 {index}/{total} 个 TeX 文件")
            usage = translator.total_usage
            self._log(
                log_path,
                "translated files={} chunks={} characters={} fallbacks={} input_tokens={} cached_input_tokens={} output_tokens={} reasoning_output_tokens={} total_tokens={} cost_usd={}".format(
                    len(tex_files),
                    total_chunks,
                    total_characters,
                    total_fallbacks,
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                    usage.reasoning_output_tokens,
                    usage.total_tokens,
                    token_cost(
                        usage.input_tokens,
                        usage.cached_input_tokens,
                        usage.output_tokens,
                    )["cost_usd"],
                ),
            )

            self._update(job, appendix_files=sorted(appendix_files))
            self._compile_and_finish(job, translated_root, fallbacks=total_fallbacks)
        except Exception as exc:
            self._log(log_path, f"ERROR: {exc}")
            if isinstance(exc, TranslationError):
                detail = str(exc)
                status = "disconnected" if self._is_codex_auth_error(detail) else "error"
                message = "Codex CLI 认证失效，请重新登录" if status == "disconnected" else detail[-500:]
                self._set_codex_status(status, message)
            self._update(job, status="failed", phase="failed", message="任务失败", error=str(exc), log_path=str(log_path))

    def _compile_and_finish(self, job: Job, translated_root: Path, *, fallbacks: int = 0) -> None:
        """Compile the translated work tree as it is on disk and package the result.

        Compile repairs call this directly, so their edits are never overwritten by a
        translation pass. Raises on compile failure once automatic repair is exhausted.
        """
        job_dir = self._job_dir(job.id)
        log_path = job_dir / "job.log"
        translated_dir = job_dir / "translated"
        artifact_dir = job_dir / "artifacts"
        self._update(job, phase="compile", progress=70, message="正在编译中文 PDF", compile_failed=True)
        compile_started = time.time()
        result = compile_latex(translated_root, artifact_dir)
        self._log(log_path, f"compile command: {' '.join(result.command)}")
        self._log(log_path, f"compile returncode: {result.returncode}")
        self._log(log_path, result.stdout)
        self._log(log_path, result.stderr)
        if result.returncode != 0 or result.pdf_path is None:
            self._update(job, quality_issues=[])
            if self._start_automatic_compile_repair(job):
                return
            raise RuntimeError("LaTeX 编译失败，请查看编译日志")

        # Exit code 0 is not enough: undefined citations or missing glyphs only appear as warnings.
        issues = inspect_build(translated_root, since=compile_started - 1)
        for issue in issues:
            self._log(log_path, f"build check: {issue.kind} ({'blocking' if issue.blocking else 'warning'}): {issue.describe()}")
        self._update(job, quality_issues=[issue.describe() for issue in issues])
        if any(issue.blocking for issue in issues) and self._start_automatic_compile_repair(job, quality=True):
            return

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
            message="翻译和编译已完成" if fallbacks == 0 else f"翻译和编译已完成（{fallbacks} 个片段保留原文）",
            pdf_path=str(result.pdf_path),
            source_path=str(source_zip),
            appendix_files=[] if job.translate_appendix else job.appendix_files,
            translate_appendix=False,
            compile_failed=False,
        )
        if job.repairs_to_learn and not any(issue.blocking for issue in issues):
            self._learn_from_repairs(job)
        self._set_codex_status("connected", "Codex CLI 可用，最近任务已完成")

    def _learn_from_repairs(self, job: Job) -> None:
        """Ask Codex to turn the repairs that fixed this build into a reusable skill entry."""

        log_path = self._job_dir(job.id) / "job.log"
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            repairs = self._repair_history(log_text)
            if not repairs:
                return
            problem_lines = re.findall(r"^(?:error: .+|! .+|build check: .+)$", log_text, re.M)
            problems = "\n".join(list(dict.fromkeys(problem_lines))[-40:]) or "(see repairs)"
            translator = CodexCliTranslator()
            output = translator.translate(
                lesson_prompt(skill_for_prompt(self.base_dir), problems, "\n\n".join(repairs[-3:])),
                cwd=self._job_dir(job.id),
            )
            lesson = parse_lesson(output)
            if lesson:
                append_lesson(self.base_dir, lesson, source=f"{job.arxiv_id} ({job.id})")
                self._log(log_path, f"repair skill updated: {lesson.splitlines()[0]}")
            else:
                self._log(log_path, "repair skill: no new lesson")
            usage = translator.total_usage
            self._update(
                job,
                repairs_to_learn=False,
                input_tokens=job.input_tokens + usage.input_tokens,
                cached_input_tokens=job.cached_input_tokens + usage.cached_input_tokens,
                output_tokens=job.output_tokens + usage.output_tokens,
                reasoning_output_tokens=job.reasoning_output_tokens + usage.reasoning_output_tokens,
                total_tokens=job.total_tokens + usage.total_tokens,
            )
        except Exception as exc:  # Learning must never fail a finished translation.
            self._log(log_path, f"repair skill update skipped: {exc}")

    def _persist_chunk_progress(
        self,
        job: Job,
        translator: CodexCliTranslator,
        translation_cache: dict[str, dict[str, dict[str, str]]],
        cache_path: Path,
    ) -> None:
        temp = cache_path.with_suffix(".tmp")
        payload = {"_version": TRANSLATION_CACHE_VERSION, **translation_cache}
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(cache_path)
        usage = translator.total_usage
        self._update(
            job,
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            total_tokens=usage.total_tokens,
        )

    @staticmethod
    def _strip_comments_in_tree(root: Path) -> None:
        for tex_file in root.rglob("*.tex"):
            source = tex_file.read_text(encoding="utf-8", errors="replace")
            cleaned = strip_latex_comments(source)
            if cleaned != source:
                tex_file.write_text(cleaned, encoding="utf-8")

