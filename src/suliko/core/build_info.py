"""Which commit this process is running, and since when.

`/health` used to say only "0.1.0", which never changes, so after a deploy the
only way to know what was live was to ask whoever had the server. A deploy here
is a pull and a service restart, and either half can be forgotten: a pull
without a restart leaves the old code answering, a restart without a pull
does the same.

So the commit is read ONCE, when the process starts, from the checkout's own
`.git` files. Read per request it would show the new commit the moment the pull
finished while the old code was still serving, which is the exact mistake it
exists to catch. Read at start, "pulled but not restarted" shows the old hash,
and `started_at` shows whether the restart came after the pull.

The files are read directly rather than by running `git`: the service runs as
a Windows service whose PATH need not include it, and a health answer must
never depend on spawning a process. `SULIKO_COMMIT` overrides it, for a
deployment that has no `.git` (a container built from an archive).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

#: The repository root: src/suliko/core/build_info.py is three levels below it.
ROOT = Path(__file__).resolve().parents[3]

#: When this process imported the module, which is when it started serving.
STARTED_AT = datetime.now(UTC).isoformat(timespec="seconds")

SHORT = 7


def _is_sha(text: str) -> bool:
    return len(text) in (40, 64) and all(ch in "0123456789abcdef" for ch in text)


def _git_dirs(root: Path) -> list[Path]:
    """The directories that hold this checkout's git state, most specific first.

    An ordinary checkout has one. A linked worktree has its own (HEAD) and
    shares the main one (refs, packed-refs), named by `commondir`.
    """
    dot_git = root / ".git"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return []
        git_dir = Path(text.removeprefix("gitdir:").strip())
        if not git_dir.is_absolute():
            git_dir = (root / git_dir).resolve()
    elif dot_git.is_dir():
        git_dir = dot_git
    else:
        return []

    directories = [git_dir]
    common = git_dir / "commondir"
    if common.is_file():
        directories.append((git_dir / common.read_text(encoding="utf-8").strip()).resolve())
    return directories


def read_commit(root: Path) -> str | None:
    """The full hash `root`'s HEAD points at, or None when it cannot be read.

    Never raises: this runs on the way to answering a health check.
    """
    try:
        directories = _git_dirs(root)
        if not directories:
            return None

        head = (directories[0] / "HEAD").read_text(encoding="utf-8").strip()
        if _is_sha(head):  # a detached HEAD holds the hash itself
            return head
        if not head.startswith("ref:"):
            return None
        ref = head.removeprefix("ref:").strip()

        for directory in directories:
            loose = directory / ref
            if loose.is_file():
                value = loose.read_text(encoding="utf-8").strip()
                if _is_sha(value):
                    return value

        # `git gc` moves refs out of their own files into one list.
        for directory in directories:
            packed = directory / "packed-refs"
            if not packed.is_file():
                continue
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith(("#", "^")) or not line.endswith(f" {ref}"):
                    continue
                value = line.split(" ", 1)[0]
                if _is_sha(value):
                    return value
    except (OSError, ValueError):
        return None
    return None


@lru_cache(maxsize=1)
def commit() -> str:
    """The short commit this process started on, or "unknown"."""
    configured = os.environ.get("SULIKO_COMMIT", "").strip().lower()
    found = configured or read_commit(ROOT) or ""
    return found[:SHORT] or "unknown"
