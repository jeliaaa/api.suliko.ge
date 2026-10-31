"""The Claude Code server's worker (`worker/claude_worker.py`).

Office is a `httpx.MockTransport` and Claude Code is a patched `subprocess.run`,
so the whole loop runs here: claim, download, translate, convert, upload, and
each way a job can end.
"""

from __future__ import annotations

import dataclasses
import io
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

import claude_worker as worker  # noqa: E402

JOB = {
    "job_id": "12.k3Jd9abcdefg",
    "order_number": 1436,
    "file_name": "ASUS ISO14001.pdf",
    "content_type": "application/pdf",
    "size_bytes": 20,
    "source_language": "ru",
    "target_language": "ka",
    "source_language_name": "Russian",
    "target_language_name": "Georgian",
    "instructions": "Spell Tamar as Tamara.",
    "attempts": 1,
    "output_file_name": "ASUS_ISO14001_Translated.docx",
    "source_path": "/api/v1/worker/jobs/12.k3Jd9abcdefg/source",
}

HTML = "<h1>სერტიფიკატი</h1><p>ISO 14001 <strong>Tamara</strong></p>"


class FakeOffice:
    """Records what the worker sends and answers like Office."""

    def __init__(self, jobs: list[dict[str, Any]]) -> None:
        self.jobs = list(jobs)
        self.calls: list[tuple[str, str]] = []
        self.reports: list[dict[str, Any]] = []
        self.uploads: list[tuple[str, bytes]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/worker")
        self.calls.append((request.method, path))
        assert request.headers["authorization"] == "Bearer secret"
        if path == "/jobs/claim":
            return httpx.Response(200, json=self.jobs.pop(0)) if self.jobs else httpx.Response(204)
        if path.endswith("/source"):
            return httpx.Response(200, content=b"%PDF-1.7 original")
        if path.endswith("/result"):
            body = request.read()
            start = body.index(b"PK\x03\x04")
            self.uploads.append((path, body[start:]))
            return httpx.Response(200, json={"status": "translated"})
        if path.endswith("/status"):
            import json

            self.reports.append(json.loads(request.read()))
            return httpx.Response(200, json={"status": "failed"})
        return httpx.Response(204)


def make(
    tmp_path: Path, jobs: list[dict[str, Any]]
) -> tuple[worker.Config, FakeOffice, worker.Office]:
    config = worker.Config(token="secret", work_dir=tmp_path / "work", poll_seconds=0)
    fake = FakeOffice(jobs)
    client = httpx.Client(
        base_url="http://office.test/api/v1/worker",
        headers={"Authorization": "Bearer secret"},
        transport=httpx.MockTransport(fake.handler),
    )
    return config, fake, worker.Office(config, client)


def fake_claude(
    monkeypatch: pytest.MonkeyPatch,
    *,
    write: str | None = HTML,
    code: int = 0,
    output: str = "done",
) -> list[dict[str, Any]]:
    """Stands in for `claude -p`: writes translation.html in its working folder."""
    seen: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        folder = Path(kwargs["cwd"])
        seen.append(
            {
                "command": command,
                "claude_md": (folder / "CLAUDE.md").read_text(encoding="utf-8"),
                "source": sorted(p.name for p in (folder / "source").iterdir()),
                "timeout": kwargs["timeout"],
            }
        )
        if write is not None:
            (folder / "translation.html").write_text(write, encoding="utf-8")
        return subprocess.CompletedProcess(command, code, stdout=output, stderr="")

    monkeypatch.setattr(worker.subprocess, "run", run)
    return seen


def test_a_job_is_translated_converted_uploaded_and_cleaned_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])
    seen = fake_claude(monkeypatch)

    assert worker.run(config, once=True, office=client) == 0

    assert [c for c in office.calls if c[1] != "/heartbeat"] == [
        ("POST", "/jobs/claim"),
        ("GET", "/jobs/12.k3Jd9abcdefg/source"),
        ("POST", "/jobs/12.k3Jd9abcdefg/result"),
    ]
    (_, docx) = office.uploads[0]
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        assert "სერტიფიკატი" in archive.read("word/document.xml").decode("utf-8")
    # What Claude was given: the languages, the order's instructions, no shell.
    assert "Georgian (ka)" in seen[0]["claude_md"]
    assert "Spell Tamar as Tamara." in seen[0]["claude_md"]
    command = seen[0]["command"]
    assert command[1] == "-p"
    assert Path(command[0]).stem.lower() == "claude" or command[0] == "claude"
    allowed = command[command.index("--allowedTools") + 1 : command.index("--max-turns")]
    assert allowed == ["Read", "Write", "Edit"]
    assert "Bash" not in command
    # A name with a space and no path of its own, so it cannot leave the folder.
    assert seen[0]["source"] == ["ASUS_ISO14001.pdf"]
    assert seen[0]["timeout"] == 25 * 60
    # Nothing of the document is left on the worker.
    assert not (tmp_path / "work" / "12.k3Jd9abcdefg").exists()


def test_nothing_waiting_ends_a_once_run(tmp_path: Path) -> None:
    config, office, client = make(tmp_path, [])
    assert worker.run(config, once=True, office=client) == 0
    assert ("POST", "/jobs/claim") in office.calls


def test_no_translation_written_fails_the_job_with_a_plain_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])
    fake_claude(monkeypatch, write=None)
    worker.run(config, once=True, office=client)
    assert office.reports == [
        {"status": "failed", "error_message": "Claude finished without writing a translation."}
    ]
    assert office.uploads == []


def test_an_empty_translation_fails_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])
    fake_claude(monkeypatch, write="<p> </p>")
    worker.run(config, once=True, office=client)
    assert office.reports[0]["status"] == "failed"
    assert "empty" in office.reports[0]["error_message"]


def test_a_claude_error_is_reported_without_quoting_what_claude_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])
    fake_claude(monkeypatch, write=None, code=1, output="could not read passport of Nino Beridze")
    worker.run(config, once=True, office=client)
    (report,) = office.reports
    assert report["status"] == "failed"
    assert "Beridze" not in report["error_message"]
    assert "exit code 1" in report["error_message"]


def test_a_usage_limit_puts_the_job_back_and_stops_a_once_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])
    fake_claude(
        monkeypatch, write=None, code=1, output="Claude usage limit reached. Try again at 5pm"
    )
    assert worker.run(config, once=True, office=client) == 2
    assert office.reports[0]["status"] == "queued"
    assert "usage limit" in office.reports[0]["error_message"].lower()


def test_a_timeout_fails_the_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, office, client = make(tmp_path, [JOB])

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(worker.subprocess, "run", run)
    worker.run(config, once=True, office=client)
    assert "25 minutes" in office.reports[0]["error_message"]
    assert office.reports[0]["status"] == "failed"


def test_a_missing_claude_install_is_said_plainly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(worker.subprocess, "run", run)
    worker.run(config, once=True, office=client)
    assert "not installed" in office.reports[0]["error_message"]


def test_a_word_source_is_given_to_claude_as_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from docx import Document

    source = io.BytesIO()
    document = Document()
    document.add_paragraph("Привет мир")
    document.save(source)
    config, office, client = make(tmp_path, [{**JOB, "file_name": "letter.docx"}])
    original = office.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/source"):
            return httpx.Response(200, content=source.getvalue())
        return original(request)

    client.http = httpx.Client(
        base_url="http://office.test/api/v1/worker",
        headers={"Authorization": "Bearer secret"},
        transport=httpx.MockTransport(handler),
    )
    seen = fake_claude(monkeypatch)
    worker.run(config, once=True, office=client)
    assert seen[0]["source"] == ["letter.docx", "letter.txt"]
    assert "letter.txt" in seen[0]["command"][2]
    assert office.uploads


def test_file_names_cannot_leave_the_job_folder() -> None:
    for hostile in (
        "..\\..\\evil.pdf",
        "../../etc/passwd",
        "C:\\Windows\\x.dll",
        "a b?.pdf",
        "...",
    ):
        name = worker.safe_name(hostile)
        assert "/" not in name and "\\" not in name and ":" not in name
        assert not name.startswith(".")
    assert worker.safe_name("ASUS ISO14001.pdf") == "ASUS_ISO14001.pdf"


def test_the_vendored_converter_is_the_one_office_uses() -> None:
    """The worker runs on another machine, so it carries its own copy of
    `domain/translation_docx.py`. Copy it again when that file changes."""
    office = (ROOT / "src/suliko/domain/translation_docx.py").read_text(encoding="utf-8")
    copy = (ROOT / "worker/translation_docx.py").read_text(encoding="utf-8")
    assert office.replace("\r\n", "\n") == copy.replace("\r\n", "\n")


def test_the_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_WORKER_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        worker.Config.from_env()


def with_rules(
    config: worker.Config, tmp_path: Path, text: str = "## Bureau rules\nUse slashes."
) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text(text, encoding="utf-8")
    return dataclasses.replace(config, rules_file=rules)


def test_the_bureau_rules_are_in_every_jobs_claude_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _office, client = make(tmp_path, [JOB])
    config = with_rules(config, tmp_path)
    seen = fake_claude(monkeypatch)

    worker.run(config, once=True, office=client)

    claude_md = seen[0]["claude_md"]
    assert "Use slashes." in claude_md
    # The order's own instructions come after them, so they win.
    assert claude_md.index("Use slashes.") < claude_md.index("Spell Tamar as Tamara.")


def test_no_rules_file_is_no_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _office, client = make(tmp_path, [JOB])
    config = dataclasses.replace(config, rules_file=tmp_path / "missing.md")
    seen = fake_claude(monkeypatch)

    assert worker.run(config, once=True, office=client) == 0
    assert "Bureau rules" not in seen[0]["claude_md"]


def test_the_shipped_rules_file_is_read_by_default() -> None:
    assert worker.DEFAULT_RULES_FILE.is_file()
    text = worker.DEFAULT_RULES_FILE.read_text(encoding="utf-8")
    assert "[-----]" in text and "თარჯიმანი:" in text


@pytest.mark.parametrize(
    ("html", "found"),
    [
        ("<p>a — b</p>", True),
        (f"<p>a {chr(0x2013)} b</p>", True),
        ("<p>a &mdash; b</p>", True),
        ("<p>[---]</p>", True),
        ("<p>[-----]</p>", True),
        ("<p>04-10-2004 EN 1317-5:2007+A2:2012</p>", False),
        ("<p>one - two</p>", False),
        ('<p style="x">fine</p><!-- --- -->', False),
    ],
)
def test_dashes_are_found_in_the_text_only(html: str, found: bool) -> None:
    assert worker.has_forbidden_dashes(html) is found


def test_a_dash_gets_one_fix_pass_and_the_fixed_text_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])
    prompts: list[str] = []
    outputs = ["<h1>ცნობა — ISO</h1>", "<h1>ცნობა, ISO</h1>"]

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        prompts.append(command[command.index("-p") + 1])
        (Path(kwargs["cwd"]) / "translation.html").write_text(outputs.pop(0), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", run)

    worker.run(config, once=True, office=client)

    assert len(prompts) == 2 and "em dash" in prompts[1]
    (_, docx) = office.uploads[0]
    with zipfile.ZipFile(io.BytesIO(docx)) as archive:
        text = archive.read("word/document.xml").decode("utf-8")
    assert "—" not in text and "ცნობა, ISO" in text


def test_a_dash_that_survives_the_fix_is_not_looped_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, office, client = make(tmp_path, [JOB])
    runs: list[int] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        runs.append(1)
        (Path(kwargs["cwd"]) / "translation.html").write_text("<p>a — b</p>", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", run)

    worker.run(config, once=True, office=client)

    assert len(runs) == 2
    assert len(office.uploads) == 1
