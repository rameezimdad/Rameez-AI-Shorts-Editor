"""
Developed by Mohammad Rameez Imdad (Rameez Scripts)
WhatsApp: https://whatsapp.rameezscripts.com/ (For Custom Projects)
YouTube: https://www.youtube.com/@rameezimdad (Subscribe for more!)

In-memory job store + background runner. No Celery/Redis: the FastAPI event loop
hands the blocking pipeline to a worker thread (asyncio.to_thread) behind a
semaphore, so N whisper jobs never load N models at once.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from collections.abc import Callable

from . import config
from .schemas import RenderOptions, Usage

log = logging.getLogger("shorts.jobs")

Status = Literal["queued", "running", "done", "error"]


@dataclass
class Job:
    id: str
    filename: str
    input_path: Path
    work_dir: Path
    output_path: Path
    options: RenderOptions
    status: Status = "queued"
    progress: int = 0
    step: str = "queued"
    error: str | None = None
    duration: float = 0.0
    language: str = ""
    usage: Usage = field(default_factory=Usage)
    log_lines: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # --- mutation helpers (called from the worker thread) ---
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.log_lines.append(line)
        if len(self.log_lines) > config.LOG_KEEP_LINES:  # bound memory
            del self.log_lines[: len(self.log_lines) - config.LOG_KEEP_LINES]
        self.updated_at = time.time()
        log.info("[%s] %s", self.id, msg)

    def set(self, step: str, progress: int) -> None:
        self.step, self.progress = step, max(0, min(100, int(progress)))
        self.updated_at = time.time()

    def start(self, step: str = "starting") -> None:
        self.status, self.error = "running", None
        self.set(step, 0)

    def finish(self) -> None:
        self.status = "done"
        self.set("done", 100)
        self.log("done")

    def fail(self, err: str) -> None:
        self.status, self.error = "error", err
        self.set("error", self.progress)
        self.log(f"ERROR: {err}")

    @property
    def busy(self) -> bool:
        return self.status in ("queued", "running")

    @property
    def plan_path(self) -> Path:
        return self.work_dir / "plan.json"

    @property
    def transcript_path(self) -> Path:
        return self.work_dir / "transcript.json"

    def to_dict(self, log_tail: int = config.LOG_KEEP_LINES) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "filename": self.filename,
            "status": self.status,
            "progress": self.progress,
            "step": self.step,
            "error": self.error,
            "duration": round(self.duration, 3),
            "language": self.language,
            "usage": self.usage.model_dump(),
            "options": self.options.model_dump(),
            "has_output": self.output_path.exists() and self.status == "done",
            "has_plan": self.plan_path.exists(),
            "log": self.log_lines[-log_tail:],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobStore:
    """dict job_id -> Job, plus the asyncio runner."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: set[Any] = set()
        self._sem: asyncio.Semaphore | None = None  # created lazily on the running loop
        self._loop: asyncio.AbstractEventLoop | None = None  # set by attach_loop() at startup

    def attach_loop(self) -> None:
        """Call once from the running event loop so submit() also works from threadpool endpoints."""
        self._loop = asyncio.get_running_loop()

    # --- crud ---
    def create(self, filename: str, options: RenderOptions) -> Job:
        """filename must already be sanitized; the upload lands at uploads/<id>_<filename>."""
        jid = uuid.uuid4().hex[:12]
        job = Job(
            id=jid,
            filename=filename,
            input_path=config.UPLOADS_DIR / f"{jid}_{filename}",
            work_dir=config.WORK_DIR / jid,
            output_path=config.OUTPUTS_DIR / f"{jid}.mp4",
            options=options,
        )
        job.work_dir.mkdir(parents=True, exist_ok=True)
        self._jobs[jid] = job
        return job

    def get(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    def all(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def remove(self, jid: str) -> bool:
        """Drop the job + every file it owns. Caller checks it's not busy."""
        job = self._jobs.pop(jid, None)
        if not job:
            return False
        shutil.rmtree(job.work_dir, ignore_errors=True)
        for p in (job.input_path, job.output_path):
            p.unlink(missing_ok=True)
        return True

    # --- runner ---
    @property
    def sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
        return self._sem

    def submit(self, job: Job, fn: Callable[..., None], *args: Any) -> None:
        """Queue `fn(*args)` on a worker thread; the job flips to running inside fn.
        Safe from the event loop AND from a sync endpoint running on Starlette's threadpool."""
        coro = self._run(job, fn, *args)
        try:
            task: Any = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # not on the loop thread
            if self._loop is None:
                coro.close()
                raise RuntimeError("job runner has no event loop - call store.attach_loop() at startup") from None
            task = asyncio.run_coroutine_threadsafe(coro, self._loop)
        job.status, job.error = "queued", None  # only after the task really exists
        job.set("waiting for a free worker", 0)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, job: Job, fn: Callable[..., None], *args: Any) -> None:
        async with self.sem:
            try:
                await asyncio.to_thread(fn, *args)
            except Exception as e:  # fn already reports its own errors; this is the safety net
                log.exception("job %s crashed outside pipeline", job.id)
                if job.status != "error":
                    job.fail(f"{type(e).__name__}: {e}")


store = JobStore()
