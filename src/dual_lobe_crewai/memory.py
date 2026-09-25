from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
import threading


@dataclass
class MemoryEntry:
    ts: float
    text: str
    pinned: bool = False


class JsonlMemoryStore:
    """Deterministic JSONL adapter with thread/process-safe writes and Unicode retrieval."""

    _thread_locks: dict[str, threading.RLock] = {}
    _thread_locks_guard = threading.Lock()

    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("DUAL_LOBE_MEMORY_PATH", ".dual_lobe_memory.jsonl"))
        self.corrupt_lines = 0
        with self._thread_locks_guard:
            self._lock = self._thread_locks.setdefault(str(self.path.resolve()), threading.RLock())

    @contextmanager
    def _file_lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._lock:
            fh = lock_path.open("a+b")
            try:
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"0"); fh.flush()
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    while True:
                        try:
                            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1); break
                        except OSError:
                            time.sleep(0.01)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fh.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                finally:
                    fh.close()

    def _entries(self) -> list[MemoryEntry]:
        if not self.path.exists():
            return []
        out, bad = [], 0
        with self._file_lock():
            try:
                lines = self.path.read_text(encoding="utf-8", errors="strict").splitlines()
            except FileNotFoundError:
                return []
        for line in lines:
            try:
                row = json.loads(line)
                out.append(MemoryEntry(**row))
            except Exception:
                bad += 1
        self.corrupt_lines += bad
        return out

    def record(self, text: str, pinned: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        row = json.dumps(asdict(MemoryEntry(time.time(), text, pinned)), ensure_ascii=False) + "\n"
        with self._file_lock():
            with self.path.open("a", encoding="utf-8", newline="") as f:
                f.write(row)
                f.flush()
                os.fsync(f.fileno())

    @staticmethod
    def _normalize(s: str) -> str:
        return unicodedata.normalize("NFKC", s or "").casefold()

    @classmethod
    def _tokens(cls, s: str) -> set[str]:
        return {x for x in re.findall(r"\w+", cls._normalize(s), flags=re.UNICODE) if len(x) >= 2}

    def search(self, query: str, limit: int = 4) -> list[str]:
        nq = self._normalize(query).strip()
        q = self._tokens(query)
        if not nq:
            return []
        scored = []
        for i, e in enumerate(self._entries()):
            nt = self._normalize(e.text)
            t = self._tokens(e.text)
            overlap = len(q & t) if q else 0
            substring = 2 if nq and nq in nt else 0
            score = overlap + substring
            if score:
                scored.append((score, e.ts, i, e.text))
        scored.sort(reverse=True)
        return [x[3] for x in scored[:limit]]

    def auto_slice(self, task: str, max_chars: int = 10_000) -> str:
        entries = self._entries()
        if not entries:
            return ""
        pinned = [e.text for e in entries if e.pinned]
        recent = [e.text for e in sorted(entries, key=lambda x: x.ts, reverse=True)[:3]]
        hits = self.search(task, limit=4)
        first = [entries[0].text] if entries else []
        seen, ordered = set(), []
        for x in pinned + recent + hits + first:
            x = x.strip()
            if x and x not in seen:
                ordered.append(x); seen.add(x)
        return "\n\n".join(f"- {x}" for x in ordered)[:max_chars]
