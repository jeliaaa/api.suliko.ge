"""Which commit is running, as `/health` reports it.

The point of the feature is that a deploy can be CHECKED instead of believed,
so the tests cover the two ways it could lie: reading the wrong thing (a ref,
a packed ref, a linked worktree) and reading it at the wrong time (per request
instead of at start).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from suliko.config import get_settings
from suliko.core import build_info
from suliko.core.build_info import read_commit

SHA = "02947e15af7839c2c5c35b3cabb451db6676f0f5"
OTHER = "fb9b421b379560f5be08d32fd96d1ef688cb2fbe"


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    build_info.commit.cache_clear()
    yield
    build_info.commit.cache_clear()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── Reading a checkout ──────────────────────────────────────────────────────


def test_a_branch_is_followed_to_its_hash(tmp_path: Path) -> None:
    _write(tmp_path / ".git" / "HEAD", "ref: refs/heads/master\n")
    _write(tmp_path / ".git" / "refs" / "heads" / "master", SHA + "\n")

    assert read_commit(tmp_path) == SHA


def test_a_packed_ref_is_found_when_git_gc_has_moved_it(tmp_path: Path) -> None:
    _write(tmp_path / ".git" / "HEAD", "ref: refs/heads/master\n")
    _write(
        tmp_path / ".git" / "packed-refs",
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{OTHER} refs/heads/other\n"
        f"{SHA} refs/heads/master\n"
        f"^{OTHER}\n",
    )

    assert read_commit(tmp_path) == SHA


def test_a_loose_ref_wins_over_a_stale_packed_one(tmp_path: Path) -> None:
    """A pull writes the loose file; the packed copy is what it replaced."""
    _write(tmp_path / ".git" / "HEAD", "ref: refs/heads/master\n")
    _write(tmp_path / ".git" / "refs" / "heads" / "master", SHA + "\n")
    _write(tmp_path / ".git" / "packed-refs", f"{OTHER} refs/heads/master\n")

    assert read_commit(tmp_path) == SHA


def test_a_detached_head_holds_the_hash_itself(tmp_path: Path) -> None:
    _write(tmp_path / ".git" / "HEAD", SHA + "\n")

    assert read_commit(tmp_path) == SHA


def test_a_linked_worktree_reads_head_from_its_own_dir_and_refs_from_the_main_one(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    _write(main / ".git" / "refs" / "heads" / "feature", SHA + "\n")
    admin = main / ".git" / "worktrees" / "feature"
    _write(admin / "HEAD", "ref: refs/heads/feature\n")
    _write(admin / "commondir", "../..\n")
    worktree = tmp_path / "feature"
    _write(worktree / ".git", f"gitdir: {admin}\n")

    assert read_commit(worktree) == SHA


def test_a_relative_gitdir_is_resolved_from_the_checkout(tmp_path: Path) -> None:
    _write(tmp_path / "real" / "HEAD", SHA + "\n")
    _write(tmp_path / "work" / ".git", "gitdir: ../real\n")

    assert read_commit(tmp_path / "work") == SHA


@pytest.mark.parametrize(
    "head",
    [
        "",
        "garbage\n",
        "ref: refs/heads/missing\n",
        "02947e1\n",  # an abbreviated hash is not one we would trust
        "ref: refs/heads/master\n",  # ...with no file behind it
    ],
)
def test_anything_unreadable_is_none_not_an_error(tmp_path: Path, head: str) -> None:
    _write(tmp_path / ".git" / "HEAD", head)

    assert read_commit(tmp_path) is None


def test_no_git_at_all_is_none(tmp_path: Path) -> None:
    assert read_commit(tmp_path) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_it_agrees_with_git_itself() -> None:
    """The real checkout, whatever shape it has (a plain clone or a worktree)."""
    asked = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607 -- fixed arguments, no input
        cwd=build_info.ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if asked.returncode != 0:
        pytest.skip("not a git checkout")

    assert read_commit(build_info.ROOT) == asked.stdout.strip()


# ── What the process reports ────────────────────────────────────────────────


def test_the_commit_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SULIKO_COMMIT", SHA)

    assert build_info.commit() == "02947e1"


def test_the_environment_overrides_a_missing_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(build_info, "ROOT", tmp_path)
    monkeypatch.setenv("SULIKO_COMMIT", "ABCDEF1234")

    assert build_info.commit() == "abcdef1"


def test_with_nothing_to_read_it_says_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(build_info, "ROOT", tmp_path)
    monkeypatch.delenv("SULIKO_COMMIT", raising=False)

    assert build_info.commit() == "unknown"


def test_it_is_read_once_so_a_pull_without_a_restart_still_shows_the_old_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole reason it is not read per request."""
    monkeypatch.setattr(build_info, "ROOT", tmp_path)
    monkeypatch.delenv("SULIKO_COMMIT", raising=False)
    _write(tmp_path / ".git" / "HEAD", OTHER + "\n")
    assert build_info.commit() == "fb9b421"

    _write(tmp_path / ".git" / "HEAD", SHA + "\n")  # the pull

    assert build_info.commit() == "fb9b421"  # no restart yet


def test_the_start_time_is_a_utc_timestamp() -> None:
    assert build_info.STARTED_AT.endswith("+00:00")


# ── /health ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    from suliko.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def test_health_says_which_commit_and_since_when(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("SULIKO_COMMIT", SHA)
    build_info.commit.cache_clear()

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["commit"] == "02947e1"
    assert body["started_at"] == build_info.STARTED_AT
    assert body["version"]
