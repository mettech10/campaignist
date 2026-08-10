"""Background execution for video renders.

Same shape as pipeline/runner.py: this module owns the thread, queue.py owns
the work. One thread drains the queue rather than one per job, because fal is
doing the rendering and the only local cost is an HTTP poll — but a campaign
with a dozen video assets should not open a dozen threads on a 512MB instance.
"""
from __future__ import annotations

import logging
import os
import socket
import threading

from . import queue

log = logging.getLogger(__name__)

# Identifies this process in a lease. If two Render instances are ever running,
# each one's claims are distinguishable and a heartbeat cannot cross over.
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"

_lock = threading.Lock()
_draining = False


def start() -> None:
    """Ensure a drain thread is running. Safe to call on every enqueue."""
    global _draining
    with _lock:
        if _draining:
            return
        _draining = True
    threading.Thread(target=_drain, name="render-drain", daemon=True).start()


def _drain() -> None:
    """Claim and render until the queue is empty, then stop.

    Stopping when empty rather than polling forever matters on Render's free
    tier: an idle thread keeps the instance awake and the dyno is not free
    forever. A new enqueue starts a fresh drain.
    """
    global _draining
    try:
        while True:
            # Return abandoned leases first, or a job stranded by a redeploy
            # would sit at the head of the queue and never be retried.
            queue.reap()

            job = queue.claim(WORKER_ID)
            if not job:
                return
            queue.process(job, WORKER_ID)
    except Exception:
        # A crash here would leave _draining stuck true and the queue frozen
        # until the next restart.
        log.exception("[render] drain loop died")
    finally:
        with _lock:
            _draining = False
