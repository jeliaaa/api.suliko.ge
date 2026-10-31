"""The Claude Translate worker: runs on the Claude Code server.

Asks Suliko Office for a job, has Claude Code translate the file, sends the Word
document back. The contract with Office is `CLAUDE-TRANSLATE.md`; setup on
Windows is `worker/README.md`.

    python claude_worker.py            loop until stopped
    python claude_worker.py --once     take at most one job, then exit

One job at a time. Nothing about a document is logged except job ids, order
numbers, sizes and statuses; the work folder of a job is deleted when it ends.

Configuration is by environment variable:

    CLAUDE_WORKER_TOKEN       required: one of Office's CLAUDE_WORKER_TOKENS
    OFFICE_API_URL            default https://api.suliko.ge
    WORKER_NAME               default: this computer's name
    WORK_DIR                  default ./work
    CLAUDE_BIN                default "claude"
    CLAUDE_MODEL              optional, passed as --model
    POLL_SECONDS              default 15: wait when no job is waiting
    JOB_TIMEOUT_MINUTES       default 25: Office puts a job back after 30
    USAGE_LIMIT_PAUSE_MINUTES default 30: stop asking after a usage limit
    SOFFICE_BIN               optional: LibreOffice's soffice, to count the pages of the
                              Word file and shrink it to the original's page count
    RULES_FILE                default translation-rules.md next to this script: the
                              bureau's standing rules, put in every job's CLAUDE.md
    KEEP_WORK                 set to 1 to keep each job's folder (debugging only)
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any

import httpx
from translation_docx import DocxConversionError, PageLayout, html_to_docx, widest_table

VERSION = "1.0"
log = logging.getLogger("claude-worker")

DEFAULT_RULES_FILE = Path(__file__).with_name("translation-rules.md")

#: What Claude Code says when the subscription's usage limit is reached.
_USAGE_LIMIT = re.compile(
    r"usage limit|limit reached|rate limit|quota|overloaded|try again at", re.I
)
#: Sent to Office as the reason: short, no file contents.
MAX_MESSAGE = 480


@dataclass(frozen=True)
class Config:
    token: str
    api_url: str = "https://api.suliko.ge"
    name: str = ""
    work_dir: Path = Path("work")
    claude_bin: str = "claude"
    model: str | None = None
    poll_seconds: float = 15
    job_timeout_minutes: float = 25
    usage_limit_pause_minutes: float = 30
    keep_work: bool = False
    #: The bureau's standing rules, a Markdown file. Missing or empty: none.
    rules_file: Path | None = None
    #: LibreOffice's `soffice`. None: looked for on this machine.
    soffice_bin: str | None = None

    @staticmethod
    def from_env() -> Config:
        token = os.environ.get("CLAUDE_WORKER_TOKEN", "").strip()
        if not token:
            raise SystemExit("CLAUDE_WORKER_TOKEN is not set.")
        return Config(
            token=token,
            api_url=os.environ.get("OFFICE_API_URL", "https://api.suliko.ge").rstrip("/"),
            name=os.environ.get("WORKER_NAME", "") or socket.gethostname(),
            work_dir=Path(os.environ.get("WORK_DIR", "work")).resolve(),
            claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
            model=os.environ.get("CLAUDE_MODEL") or None,
            poll_seconds=float(os.environ.get("POLL_SECONDS", "15")),
            job_timeout_minutes=float(os.environ.get("JOB_TIMEOUT_MINUTES", "25")),
            usage_limit_pause_minutes=float(os.environ.get("USAGE_LIMIT_PAUSE_MINUTES", "30")),
            keep_work=os.environ.get("KEEP_WORK") == "1",
            rules_file=Path(os.environ.get("RULES_FILE") or DEFAULT_RULES_FILE).resolve(),
            soffice_bin=os.environ.get("SOFFICE_BIN") or None,
        )


class UsageLimitError(Exception):
    """Claude Code refused because the usage limit was reached: try again later."""


class JobFailedError(Exception):
    """This job cannot be translated; the message is what the person is shown."""


# ── Talking to Office ────────────────────────────────────────────────────────


class Office:
    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        self.config = config
        self.http = client or httpx.Client(
            base_url=f"{config.api_url}/api/v1/worker",
            headers={
                "Authorization": f"Bearer {config.token}",
                "X-Worker-Name": config.name,
            },
            timeout=httpx.Timeout(120.0, connect=15.0),
        )

    def claim(self) -> dict[str, Any] | None:
        response = self.http.post("/jobs/claim")
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]

    def download(self, job_id: str, target: Path) -> None:
        with self.http.stream("GET", f"/jobs/{job_id}/source") as response:
            response.raise_for_status()
            with target.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)

    def result(self, job_id: str, name: str, content: bytes) -> None:
        response = self.http.post(
            f"/jobs/{job_id}/result",
            files={
                "file": (
                    name,
                    content,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        response.raise_for_status()

    def report(self, job_id: str, status: str, message: str) -> None:
        response = self.http.post(
            f"/jobs/{job_id}/status",
            json={"status": status, "error_message": message[:MAX_MESSAGE]},
        )
        # 409: Office already moved the job (put it back, or it was finished).
        if response.status_code != 409:
            response.raise_for_status()

    def heartbeat(self) -> None:
        self.http.post("/heartbeat", json={"detail": f"claude-worker {VERSION}"})


# ── Preparing the job's folder ───────────────────────────────────────────────

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str, fallback: str = "file") -> str:
    """A name that is safe as a file on Windows and cannot leave its folder."""
    cleaned = _UNSAFE.sub("_", value).strip("._") or fallback
    return cleaned[:80]


def readable_source(path: Path) -> Path:
    """The file as something Claude Code can read: PDFs and images as they are,
    Word and Excel files as text next to the original."""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        from docx import Document

        document = Document(str(path))
        lines = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
        text_path = path.with_suffix(".txt")
        text_path.write_text("\n".join(lines), encoding="utf-8")
        return text_path
    if suffix in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise JobFailedError("Excel files need the openpyxl package on the worker.") from exc
        workbook = load_workbook(str(path), read_only=True, data_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f"# {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                if any(cell is not None for cell in row):
                    lines.append(" | ".join("" if cell is None else str(cell) for cell in row))
        text_path = path.with_suffix(".txt")
        text_path.write_text("\n".join(lines), encoding="utf-8")
        return text_path
    return path


def read_rules(config: Config) -> str:
    """The bureau's standing rules, or "" when there is no such file."""
    if config.rules_file is None or not config.rules_file.is_file():
        return ""
    return config.rules_file.read_text(encoding="utf-8").strip()


def build_claude_md(job: dict[str, Any], rules: str = "") -> str:
    instructions = (job.get("instructions") or "").strip()
    bureau = f"\n{rules}\n" if rules else ""
    return f"""# Translation job

You translate one client document for a translation bureau.

- Source language: {job["source_language_name"]} ({job["source_language"]})
- Target language: {job["target_language_name"]} ({job["target_language"]})

## Rules

- Translate everything on the document, faithfully and completely. Do not summarise,
  do not add, do not leave parts out, do not comment.
- Keep the structure: headings, paragraphs, lists and tables, in the same order.
- Stamps, seals, signatures, handwriting and letterheads: describe them where they
  are, as the bureau rules below say (between slashes). Without such a rule use square
  brackets, for example [seal: Ministry of Justice of Georgia], [signature],
  [illegible]. Never guess at text you cannot read.
- Keep numbers, dates, codes and addresses exactly as written. Do not convert
  calendar or number formats.
- Write proper names as the instructions below say. Otherwise transliterate them
  by the usual conventions of the target language and do not translate them.
- The result is a draft for a human translator to check.
{bureau}
## Output

Write the translation to `translation.html` in this folder, and nothing else.
It is an HTML fragment in UTF-8 using only these tags: h1 to h6, p, br, strong, em,
u, ul, ol, li, table, thead, tbody, tr, th, td, and span with `style` limited to
`color`, `background-color` and `text-align`. No scripts, no classes, no images, no
links, no `<html>` or `<body>` wrapper.

If the original is landscape (its content is wider than tall, such as a long table,
even when the scan is turned sideways), make the first line of the file the comment
`<!--page: landscape-->`. The worker then sets a landscape page and fits the table to
it. Do nothing about sizes or margins: that is done for you.

## Instructions from the order

{instructions or "None."}
"""


def build_prompt(source: Path, job: dict[str, Any]) -> str:
    return (
        f"Read the file source/{source.name} and translate it from "
        f"{job['source_language_name']} into {job['target_language_name']}, following "
        "CLAUDE.md in this folder exactly. Write the result to translation.html. "
        "Reply with the single word done when it is written."
    )


# ── Running Claude Code ──────────────────────────────────────────────────────


def run_claude(config: Config, folder: Path, prompt: str) -> None:
    # On Windows an npm install is `claude.cmd`, which CreateProcess only finds by
    # its full name.
    command = [
        shutil.which(config.claude_bin) or config.claude_bin,
        "-p",
        prompt,
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        "Read",
        "Write",
        "Edit",
        "--max-turns",
        "40",
    ]
    if config.model:
        command += ["--model", config.model]
    try:
        done = subprocess.run(  # noqa: S603 -- the command is built here, not from a job
            command,
            cwd=folder,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.job_timeout_minutes * 60,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise JobFailedError(
            f"Claude did not finish within {config.job_timeout_minutes:g} minutes."
        ) from exc
    except FileNotFoundError as exc:
        raise JobFailedError("Claude Code is not installed on the worker.") from exc

    output = f"{done.stdout}\n{done.stderr}"
    if done.returncode != 0:
        if _USAGE_LIMIT.search(output):
            raise UsageLimitError(_USAGE_LIMIT.search(output).group(0))  # type: ignore[union-attr]
        # What Claude said is not shown to staff: it can quote the document.
        log.warning("claude exited with %s", done.returncode)
        raise JobFailedError(f"Claude Code stopped with an error (exit code {done.returncode}).")


# ── The Word file: page, type size, page count ───────────────────────────────

#: A table with this many columns does not fit a portrait page.
WIDE_COLUMNS = 10

_PAGE_DIRECTIVE = re.compile(r"^\s*<!--\s*page\s*:\s*(landscape|portrait)\s*-->", re.IGNORECASE)

_SOFFICE_PLACES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def declared_orientation(html: str) -> bool | None:
    """True for landscape, False for portrait, None when Claude said nothing."""
    found = _PAGE_DIRECTIVE.match(html)
    return None if found is None else found.group(1).lower() == "landscape"


def page_layouts(*, wide: bool, landscape: bool) -> list[PageLayout]:
    """From the roomiest page to the tightest. Margins go first, then type size;
    a wide table may go down to 8 pt, other text never below 10."""
    steps = (
        ((10.0, 1.2), (9.0, 1.2), (8.5, 1.0), (8.0, 1.0))
        if wide
        else ((11.0, 2.0), (11.0, 1.5), (10.5, 1.5), (10.0, 1.5))
    )
    return [PageLayout(landscape=landscape, size=size, margin_cm=margin) for size, margin in steps]


def find_soffice(config: Config) -> str | None:
    for candidate in (config.soffice_bin, shutil.which("soffice"), *_SOFFICE_PLACES):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def source_pages(path: Path) -> int | None:
    """How many pages the original has, when that is known: a PDF's, or 1 for a
    picture. A Word or Excel source has no page count that means anything."""
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        return 1
    if suffix != ".pdf":
        return None
    try:
        from pypdf import PdfReader

        return len(PdfReader(path).pages) or None
    except Exception:
        return None


def count_pages(docx: bytes, soffice: str) -> int | None:
    """The Word file's pages as LibreOffice lays it out; None if that failed.
    The file is in a temporary folder only while this runs."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "t.docx"
        source.write_bytes(docx)
        command = [
            soffice,
            f"-env:UserInstallation={(Path(folder) / 'profile').as_uri()}",
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            folder,
            str(source),
        ]
        try:
            subprocess.run(  # noqa: S603 -- built here, not from a job
                command, capture_output=True, timeout=180, stdin=subprocess.DEVNULL, check=False
            )
            return len(PdfReader(Path(folder) / "t.pdf").pages)
        except Exception:
            return None


def make_docx(config: Config, html: str, *, language: str, pages: int | None) -> bytes:
    """The translation as a Word file that suits its content.

    A wide table gets a landscape page and columns sized to what they hold; a
    document Claude marked landscape gets a landscape page. With LibreOffice on
    the machine and the original's page count known, the page is tightened step
    by step until the Word file is no longer than the original. Without it, a
    wide table gets one fixed step. A plain document is left exactly as before.
    """
    declared = declared_orientation(html)
    columns = widest_table(html)
    wide = columns >= WIDE_COLUMNS
    landscape = declared if declared is not None else wide
    layouts = page_layouts(wide=wide, landscape=landscape)
    soffice = find_soffice(config)

    if pages is None or soffice is None:
        if not (wide or landscape):
            return html_to_docx(html, language=language)
        step = (3 if columns >= 20 else 1) if wide else 0
        return html_to_docx(html, language=language, layout=layouts[step])

    docx = b""
    for layout in layouts:
        docx = html_to_docx(html, language=language, layout=layout)
        fitted = count_pages(docx, soffice)
        if fitted is None or fitted <= pages:
            break
        log.info("%s pages at %s pt, the original has %s", fitted, layout.size, pages)
    return docx


_DASHES = re.compile("[\u2014\u2013]|-{3,}")


def has_forbidden_dashes(html: str) -> bool:
    """An em dash, an en dash or three hyphens in the text of the translation.

    The bureau never uses them in a translation. Markup is not looked at (an HTML
    comment or an attribute is not text), and a single hyphen is data, not
    punctuation: dates, codes and numbers keep theirs.
    """
    text = re.sub(r"<[^>]*>", " ", unescape(html))
    return _DASHES.search(text) is not None


def fix_prompt() -> str:
    return (
        "translation.html contains an em dash, an en dash or three hyphens in a row, which "
        "CLAUDE.md forbids. Read translation.html and rewrite only those places as "
        "CLAUDE.md says (a blank field is [-----]; punctuation becomes a comma, a colon, "
        "brackets or two sentences). Change nothing else. Reply with the single word done."
    )


def read_translation(folder: Path) -> str:
    path = folder / "translation.html"
    if not path.is_file():
        raise JobFailedError("Claude finished without writing a translation.")
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(re.sub(r"<[^>]+>|\s+", "", text)) < 3:
        raise JobFailedError("The translation Claude wrote is empty.")
    return text


# ── One job ──────────────────────────────────────────────────────────────────


def process(config: Config, office: Office, job: dict[str, Any]) -> None:
    job_id = job["job_id"]
    folder = config.work_dir / safe_name(job_id, "job")
    source_dir = folder / "source"
    try:
        if folder.exists():
            shutil.rmtree(folder)
        source_dir.mkdir(parents=True)
        original = source_dir / safe_name(job["file_name"])
        office.download(job_id, original)
        readable = readable_source(original)
        (folder / "CLAUDE.md").write_text(
            build_claude_md(job, read_rules(config)), encoding="utf-8"
        )

        run_claude(config, folder, build_prompt(readable, job))
        html = read_translation(folder)
        if has_forbidden_dashes(html):
            # Once: asked to fix only those places. If a dash is still there the
            # draft goes out as it is; the translator checks it anyway.
            log.info("job %s: asking Claude to remove dashes", job_id)
            run_claude(config, folder, fix_prompt())
            html = read_translation(folder)
        try:
            docx = make_docx(
                config, html, language=job["target_language"], pages=source_pages(original)
            )
        except DocxConversionError as exc:
            raise JobFailedError("The translation could not be made into a Word file.") from exc
        office.result(job_id, job["output_file_name"], docx)
        log.info("job %s done (%d bytes)", job_id, len(docx))
    except UsageLimitError:
        office.report(job_id, "queued", "Claude's usage limit was reached. Retrying later.")
        raise
    except JobFailedError as exc:
        log.warning("job %s failed: %s", job_id, exc)
        office.report(job_id, "failed", str(exc))
    except httpx.HTTPError as exc:
        # Office or the network failed mid-job. If Office cannot be told, it puts
        # the job back by itself after 30 minutes.
        log.warning("job %s: Office call failed: %s", job_id, type(exc).__name__)
        with contextlib.suppress(httpx.HTTPError):
            office.report(job_id, "failed", "The translation server could not reach Office.")
    except Exception as exc:
        log.exception("job %s crashed", job_id)
        with contextlib.suppress(httpx.HTTPError):
            office.report(
                job_id, "failed", f"Unexpected error in the worker ({type(exc).__name__})."
            )
    finally:
        if not config.keep_work:
            shutil.rmtree(folder, ignore_errors=True)


class Heartbeat(threading.Thread):
    """Tells Office the worker is alive while a long job runs."""

    def __init__(self, office: Office, every: float = 60.0) -> None:
        super().__init__(daemon=True)
        self.office = office
        self.every = every
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(self.every):
            with contextlib.suppress(httpx.HTTPError):
                self.office.heartbeat()


def run(config: Config, *, once: bool = False, office: Office | None = None) -> int:
    office = office or Office(config)
    config.work_dir.mkdir(parents=True, exist_ok=True)
    beat = Heartbeat(office)
    beat.start()
    log.info("worker %s started, asking %s", config.name, config.api_url)
    try:
        while True:
            try:
                job = office.claim()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    log.error("Office refused the token (401). Check CLAUDE_WORKER_TOKEN.")
                else:
                    log.warning("claim failed: HTTP %s", exc.response.status_code)
                job = None
                pause = 60.0
            except httpx.HTTPError as exc:
                log.warning("claim failed: %s", type(exc).__name__)
                job = None
                pause = 60.0
            else:
                pause = config.poll_seconds

            if job is not None:
                log.info(
                    "job %s: order %s, %s (%s to %s, attempt %s)",
                    job["job_id"],
                    job["order_number"],
                    job["file_name"],
                    job["source_language"],
                    job["target_language"],
                    job["attempts"],
                )
                try:
                    process(config, office, job)
                except UsageLimitError:
                    log.warning(
                        "usage limit reached: pausing %g minutes", config.usage_limit_pause_minutes
                    )
                    if once:
                        return 2
                    time.sleep(config.usage_limit_pause_minutes * 60)
                if once:
                    return 0
                continue  # look for the next one at once
            if once:
                return 0
            time.sleep(pause)
    except KeyboardInterrupt:
        return 0
    finally:
        beat.stop.set()


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Translate worker")
    parser.add_argument("--once", action="store_true", help="take at most one job, then exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    return run(Config.from_env(), once=args.once)


if __name__ == "__main__":
    sys.exit(main())
