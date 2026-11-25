"""Keep scheduled updates clear of commands running from the same checkout."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path


def _lock_file(root: Path) -> Path | None:
    git_dir = root / ".git"
    if git_dir.is_file():
        line = git_dir.read_text(encoding="utf-8").strip()
        if not line.startswith("gitdir: "):
            return None
        git_dir = root / line.removeprefix("gitdir: ")
    return git_dir / "sb-controller-in-use.lock" if git_dir.is_dir() else None


@contextmanager
def open_lock(root: Path):
    try:
        path = _lock_file(root)
        file = path.open("a+b") if path else None
    except (OSError, UnicodeError):
        file = None
    try:
        yield file
    finally:
        if file is not None:
            file.close()


@contextmanager
def exclusive(root: Path):
    with open_lock(root) as file:
        if file is None:
            yield False
            return
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
