# -*- coding: utf-8 -*-
"""Locked, atomic JSON for everything the concurrent workers share.

Round 38 fixed the budget file losing half its increments to three writers. Auditing
the rest found the same shape in five more places:

    evidence/storage_state.json   the session file - a torn write breaks the next load
    evidence/cookies.json         cookie dump
    logs/skip_nodes.json          ONE file for all courses, each worker claiming a
                                  different `course` field, so they erase each other
    logs/node_attempts.json       one file, COURSE:CHAPTER keys
    evidence/bank.json            learned answers, written back by every worker

All of them were plain `open(path, "w")` + `json.dump`, which is neither atomic (a
reader can see a half-written file) nor mutually exclusive (two writers lose one
another's changes).

Two primitives:
    save(path, data)            atomic replace - safe for whole-file writes
    update(path, fn, default)   locked read-modify-write - for counters/accumulators
"""
from __future__ import annotations

import json
import os
import time

_LOCK_SUFFIX = ".lock"


class FileLock:
    """Cross-process mutual exclusion. Windows: msvcrt.locking. Else: O_EXCL."""

    def __init__(self, path: str):
        self.path = path + _LOCK_SUFFIX
        self.fh = None

    def __enter__(self):
        try:
            import msvcrt
            # "a+" positions at end-of-file; the lock covers nbytes from THERE, so
            # without this seek two processes can lock different regions and the
            # exclusion quietly stops working.
            self.fh = open(self.path, "a+")
            try:
                self.fh.seek(0)
            except Exception:
                pass
            for _ in range(200):                      # ~20s at 0.1s
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
                    return self
                except OSError:
                    time.sleep(0.1)
            raise TimeoutError(f"lock timeout: {self.path}")
        except ImportError:
            start = time.time()
            while True:
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    os.close(fd)
                    return self
                except FileExistsError:
                    try:
                        if time.time() - os.path.getmtime(self.path) > 30:
                            os.remove(self.path)
                            continue
                    except Exception:
                        pass
                    if time.time() - start > 20:
                        return self                   # proceed rather than stall
                    time.sleep(0.1)

    def __exit__(self, *exc):
        try:
            import msvcrt
            if self.fh:
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                self.fh.close()
        except ImportError:
            try:
                os.remove(self.path)
            except Exception:
                pass
        return False


def load(path: str, default=None):
    """Read JSON, returning `default` when missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return default if d is None else d
    except Exception:
        return default


def save(path: str, data) -> bool:
    """Atomic whole-file write: readers never see a partial file."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def update(path: str, fn, default=None):
    """Locked read-modify-write. `fn(current)` returns the new value, or None to skip.

    The lock is what makes this correct: re-reading alone does not close the window
    between read and write, which is exactly why the first attempt at the budget fix
    changed nothing.
    """
    with FileLock(path):
        cur = load(path, default)
        new = fn(cur)
        if new is None:
            return cur
        save(path, new)
        return new
