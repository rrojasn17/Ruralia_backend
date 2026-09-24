from __future__ import annotations

import os
import time
from pathlib import Path

from config import NOTIFICATION_POLL_SECONDS

poll_seconds = max(1, int(os.getenv("MODULE_WORKER_POLL_SECONDS", str(NOTIFICATION_POLL_SECONDS))))
path = Path(
    os.getenv(
        "MODULE_WORKER_HEARTBEAT_PATH",
        os.getenv("NOTIFICATION_HEARTBEAT_PATH", "/tmp/navia-module-worker-heartbeat"),
    )
)
max_age = max(90, poll_seconds * 3)
try:
    age = time.time() - float(path.read_text(encoding="utf-8").strip())
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if age <= max_age else 1)
