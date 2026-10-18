"""Background workers started with the web server.

* ``TaskWorker`` - runs heavy pipeline tasks one at a time, each in its own subprocess so
  the API stays responsive and a task can be cancelled by killing its process group.
* ``PublishScheduler`` - posts due ``PublishJob`` rows to the platforms.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import threading
import time
from datetime import timedelta

from sqlmodel import select

from .db import Account, Clip, PublishJob, Task, Video, kv_get, session_scope, update_row, utcnow

log = logging.getLogger(__name__)

PUBLISH_PAUSED_KEY = "publish_paused"
MAX_AUTO_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=2)
CLEANUP_INTERVAL = 3600.0


def _task_entry(task_id: str) -> None:  # runs in the child process
    if hasattr(os, "setsid"):
        try:
            os.setsid()  # own process group -> we can kill ffmpeg children on cancel
        except OSError:
            pass
    from .db import reset_engine
    from .logging_setup import setup_logging

    setup_logging()
    reset_engine()
    from .pipeline.runner import run_task

    run_task(task_id)


class TaskWorker(threading.Thread):
    def __init__(self, poll_interval: float = 1.0):
        super().__init__(name="task-worker", daemon=True)
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self.current_task_id: str | None = None
        self._ctx = mp.get_context("spawn")

    def stop(self) -> None:
        self._stop_event.set()

    def recover(self) -> None:
        """Tasks left 'running' by a crash/restart go back to the queue."""
        with session_scope() as s:
            for t in s.exec(select(Task).where(Task.status == "running")).all():
                t.status = "queued"
                t.message = "Restarted after interruption"
                s.add(t)
                if t.kind == "process_video" and t.video_id:
                    v = s.get(Video, t.video_id)
                    if v:
                        v.status = "queued"
                        s.add(v)
            s.commit()

    def _next_task(self) -> Task | None:
        with session_scope() as s:
            return s.exec(select(Task).where(Task.status == "queued").order_by(Task.created_at)).first()

    def run(self) -> None:
        self.recover()
        while not self._stop_event.is_set():
            task = self._next_task()
            if task is None:
                self._stop_event.wait(self.poll_interval)
                continue
            self._run(task)

    def _run(self, task: Task) -> None:
        self.current_task_id = task.id
        proc = self._ctx.Process(target=_task_entry, args=(task.id,), name=f"task-{task.id}")
        proc.start()
        cancelled = False
        while proc.is_alive():
            proc.join(timeout=1.0)
            with session_scope() as s:
                t = s.get(Task, task.id)
                if t is not None and t.cancel_requested and proc.is_alive():
                    cancelled = True
                    self._kill(proc)
            if self._stop_event.is_set() and proc.is_alive():
                self._kill(proc)
        self.current_task_id = None
        with session_scope() as s:
            t = s.get(Task, task.id)
            if t is None:
                return
            if cancelled:
                t.status, t.finished_at, t.message = "canceled", utcnow(), "Canceled"
                if t.kind == "process_video" and t.video_id:
                    v = s.get(Video, t.video_id)
                    if v:
                        v.status, v.stage = "canceled", None
                        s.add(v)
                if t.kind == "render_clip" and t.clip_id:
                    c = s.get(Clip, t.clip_id)
                    if c and c.status == "rendering":
                        c.status = "ready" if c.file_path else "failed"
                        s.add(c)
            elif t.status in ("running", "queued"):
                # Child died without reporting (segfault, OOM kill...).
                t.status, t.finished_at = "failed", utcnow()
                t.error = t.error or f"Worker process exited with code {proc.exitcode}"
                if t.kind == "process_video" and t.video_id:
                    v = s.get(Video, t.video_id)
                    if v:
                        v.status, v.error = "failed", t.error
                        s.add(v)
            s.add(t)
            s.commit()

    @staticmethod
    def _kill(proc) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        proc.join(timeout=10)
        if proc.is_alive():
            proc.kill()


class PublishScheduler(threading.Thread):
    def __init__(self, poll_interval: float = 5.0):
        super().__init__(name="publish-scheduler", daemon=True)
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def recover(self) -> None:
        with session_scope() as s:
            for j in s.exec(select(PublishJob).where(PublishJob.status == "running")).all():
                # We can't know whether the upload went through; surface it instead of double-posting.
                j.status = "failed"
                j.error = "Interrupted by a restart while uploading - check the platform, then retry if needed"
                s.add(j)
            s.commit()

    def run(self) -> None:
        self.recover()
        last_cleanup = 0.0
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # keep the loop alive
                log.exception("publish scheduler tick failed")
            if time.time() - last_cleanup > CLEANUP_INTERVAL:
                last_cleanup = time.time()
                try:
                    from .cleanup import run_cleanup

                    run_cleanup()
                except Exception:
                    log.exception("retention cleanup failed")
            self._stop_event.wait(self.poll_interval)

    def tick(self) -> int:
        if kv_get(PUBLISH_PAUSED_KEY, False):
            return 0
        now = utcnow()
        with session_scope() as s:
            due = s.exec(
                select(PublishJob)
                .where(PublishJob.status == "scheduled", PublishJob.scheduled_at <= now)
                .order_by(PublishJob.scheduled_at)
            ).all()
            for j in due:
                j.status = "running"
                s.add(j)
            s.commit()
        threads = [threading.Thread(target=execute_publish_job, args=(j.id,), daemon=True) for j in due]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return len(due)


def execute_publish_job(job_id: str) -> None:
    from .db import load_config
    from .publishers import PublishError, get_publisher

    with session_scope() as s:
        job = s.get(PublishJob, job_id)
        clip = s.get(Clip, job.clip_id) if job else None
        account = s.get(Account, job.account_id) if job else None
    if job is None:
        return
    attempts = job.attempts + 1
    if clip is None or account is None or not clip.file_path:
        update_row(PublishJob, job_id, status="failed", attempts=attempts,
                   error="Clip or account no longer exists / clip not rendered")
        return
    cfg = load_config()
    try:
        publisher = get_publisher(account, cfg)
        result = publisher.publish(clip, test=job.test)
    except PublishError as e:
        retry = e.retryable and attempts < MAX_AUTO_ATTEMPTS
        update_row(
            PublishJob, job_id,
            status="scheduled" if retry else "failed",
            attempts=attempts,
            error=str(e)[:2000] + (f" (auto-retry {attempts}/{MAX_AUTO_ATTEMPTS - 1})" if retry else ""),
            scheduled_at=utcnow() + RETRY_DELAY if retry else job.scheduled_at,
        )
        update_row(Account, account.id, last_error=str(e)[:500])
        return
    except Exception as e:  # unexpected bug/network error: retry a couple of times
        log.exception("publish job %s crashed", job_id)
        retry = attempts < MAX_AUTO_ATTEMPTS
        update_row(PublishJob, job_id, status="scheduled" if retry else "failed", attempts=attempts,
                   error=f"{e.__class__.__name__}: {e}"[:2000]
                   + (f" (auto-retry {attempts}/{MAX_AUTO_ATTEMPTS - 1})" if retry else ""),
                   scheduled_at=utcnow() + RETRY_DELAY if retry else job.scheduled_at)
        return
    update_row(PublishJob, job_id, status="published", attempts=attempts, external_id=result.external_id,
               url=result.url, error=result.note, published_at=utcnow())
    update_row(Account, account.id, last_error=None)


class Workers:
    def __init__(self) -> None:
        self.tasks = TaskWorker()
        self.publisher = PublishScheduler()

    def start(self) -> None:
        self.tasks.start()
        self.publisher.start()

    def stop(self) -> None:
        self.tasks.stop()
        self.publisher.stop()


def wait_for(predicate, timeout: float = 30.0, interval: float = 0.2) -> bool:
    """Test helper."""
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False
