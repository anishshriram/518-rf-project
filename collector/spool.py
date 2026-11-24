"""On-disk FIFO of unsent batches, one JSON object per line.

A small sidecar file records how far the queue has been consumed, so a restart
does not resend the whole backlog.
"""

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class Spool:
    def __init__(self, path: str):
        self._path = Path(path)
        self._pos_path = Path(f"{path}.pos")
        self._rejected_path = Path(f"{path}.rejected")
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.touch()
        self._offset = self._load_offset()
        with self._path.open("rb") as f:
            f.seek(self._offset)
            self._count = sum(1 for _ in f)

    def __len__(self) -> int:
        return self._count

    def append(self, batch: dict) -> None:
        with self._lock, self._path.open("a+b") as f:
            # Repair a line cut short by a crash so it cannot swallow this one.
            if f.seek(0, os.SEEK_END) > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write(json.dumps(batch).encode() + b"\n")
            f.flush()
            os.fsync(f.fileno())
            self._count += 1

    def peek(self) -> dict | None:
        """Return the oldest batch without removing it, or None if empty."""
        with self._lock:
            while self._has_data():
                with self._path.open("rb") as f:
                    f.seek(self._offset)
                    line = f.readline()
                try:
                    batch = json.loads(line)
                    if isinstance(batch, dict):
                        return batch
                except ValueError:
                    pass
                logger.warning("Skipping corrupt spool entry: %r", line[:200])
                self._advance()
            return None

    def pop(self) -> None:
        """Remove the batch last returned by peek()."""
        with self._lock:
            if self._has_data():
                self._advance()

    def reject(self, batch: dict) -> None:
        """Keep a batch the server refuses, outside the queue."""
        with self._lock, self._rejected_path.open("a") as f:
            f.write(json.dumps(batch) + "\n")

    def _has_data(self) -> bool:
        return self._offset < self._path.stat().st_size

    def _advance(self) -> None:
        with self._path.open("rb") as f:
            f.seek(self._offset)
            f.readline()
            self._offset = f.tell()
        if not self._has_data():
            self._count = 0
            # Reset the position first: a crash in between resends (the server
            # drops duplicates) instead of skipping new data.
            self._offset = 0
            self._pos_path.unlink(missing_ok=True)
            os.truncate(self._path, 0)
        else:
            self._count = max(1, self._count - 1)
            temp = Path(f"{self._pos_path}.tmp")
            temp.write_text(str(self._offset))
            temp.replace(self._pos_path)

    def _load_offset(self) -> int:
        try:
            offset = int(self._pos_path.read_text())
        except (FileNotFoundError, ValueError):
            return 0
        return offset if 0 <= offset <= self._path.stat().st_size else 0
