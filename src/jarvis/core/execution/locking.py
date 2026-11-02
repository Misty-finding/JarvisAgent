from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def execution_lock(state_dir: Path, *, wait_seconds: float = 0) -> Iterator[None]:
    """Cross-process lock shared by Core runs and user review/apply commands."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "execution.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    getattr(fcntl, "flock")(
                        handle.fileno(),
                        getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"),
                    )
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Session execution is busy; wait before review/apply or retrying."
                    ) from exc
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_UN"))
