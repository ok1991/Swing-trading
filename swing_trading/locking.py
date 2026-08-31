"""Cross-platform single-instance lock for scheduled Swing executions."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator


@contextmanager
def execution_lock(path: Path, stale_after_minutes: int = 120) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        age_seconds = datetime.now().timestamp() - path.stat().st_mtime
        if age_seconds > max(5, int(stale_after_minutes)) * 60:
            path.unlink(missing_ok=True)
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise RuntimeError(f"another Swing execution holds {path}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "started_at": datetime.now().astimezone().isoformat(
                            timespec="seconds"
                        ),
                    }
                )
            )
        yield
    finally:
        path.unlink(missing_ok=True)


__all__ = ["execution_lock"]
