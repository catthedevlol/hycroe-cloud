"""
Background worker system.

Uses threading for simplicity — swap queue.put() with Redis/RQ for multi-worker.

WHY loud failures: the original system silently swallowed worker failures.
If the worker thread dies or never starts, enqueue() now raises RuntimeError
so callers (e.g. the VPS create endpoint) can catch it and surface a proper
503 rather than leaving a VPS stuck in 'building' forever.
"""
import json
import uuid
import logging
import threading
from datetime import datetime
from queue import Queue, Empty
from typing import Callable

logger = logging.getLogger(__name__)

# Simple in-process job queue — replace with Redis + RQ/Celery for multi-worker
_job_queue: Queue = Queue()
_running = False
_worker_thread: threading.Thread | None = None


def is_worker_alive() -> bool:
    """Return True if the background worker thread is alive."""
    return _worker_thread is not None and _worker_thread.is_alive()


def enqueue(job_type: str, payload: dict) -> str:
    """
    Add a job to the queue. Returns job_id.

    Raises RuntimeError if the worker thread is not running so callers
    can catch the failure and surface a proper error (not silently drop jobs).
    """
    if not is_worker_alive():
        logger.critical(
            "enqueue(%s): worker thread is NOT running — job will NOT be processed! "
            "This is a critical system failure. Check worker startup logs.",
            job_type,
        )
        raise RuntimeError(
            f"Worker thread is not running — cannot process job '{job_type}'. "
            "The system administrator must investigate worker startup errors."
        )

    job_id = str(uuid.uuid4())
    _job_queue.put({"job_id": job_id, "type": job_type, "payload": payload})
    logger.info("Job enqueued: %s [%s]", job_type, job_id)
    return job_id


def _worker_loop(handlers: dict):
    global _running
    _running = True
    logger.info("Worker thread started")
    while _running:
        try:
            job = _job_queue.get(timeout=2)
        except Empty:
            continue
        jtype = job.get("type")
        handler: Callable = handlers.get(jtype)
        if handler:
            try:
                logger.info("Running job %s [%s]", jtype, job["job_id"])
                handler(job["payload"])
            except Exception as e:
                logger.error("Job %s [%s] failed: %s", jtype, job["job_id"], e, exc_info=True)
        else:
            logger.warning("No handler for job type: %s [%s]", jtype, job.get("job_id", "?"))
        _job_queue.task_done()
    logger.info("Worker thread exiting")


def start_worker(handlers: dict) -> threading.Thread:
    """Start the background worker thread."""
    global _worker_thread
    t = threading.Thread(target=_worker_loop, args=(handlers,), daemon=True, name="panel-worker")
    t.start()
    _worker_thread = t
    logger.info("Worker thread started (tid=%d)", t.ident or -1)
    return t


def stop_worker():
    global _running
    _running = False
    if _worker_thread:
        _worker_thread.join(timeout=10)
        logger.info("Worker thread stopped")
