# Claude Translate worker (Windows)

Runs on the Claude Code server. It asks Suliko Office for jobs, has Claude Code
translate each file and sends the Word document back. The contract with Office is
`../CLAUDE-TRANSLATE.md`.

## 1. Prerequisites on the Claude Code server

1. **Python 3.11 or newer** (python.org, tick "Add to PATH").
2. **Claude Code**, signed in. In a normal PowerShell, as the Windows user that
   will run the worker:
   ```
   claude
   ```
   Follow the login, then type `/exit`. A worker cannot answer a login prompt, so
   this must work first. Check: `claude -p "Reply with the word ok"` prints `ok`.
3. The **token**: the same string that is in Office's `.env` as
   `CLAUDE_WORKER_TOKENS`.

## 2. Install

Copy this `worker` folder to `C:\claude-worker` (the `.py` files, `translation-rules.md`
and `requirements.txt`), then:

```
cd C:\claude-worker
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 3. Try it once

```
$env:CLAUDE_WORKER_TOKEN = "<the token>"
.\.venv\Scripts\python.exe claude_worker.py --once
```

With nothing queued it asks Office once and exits. To test properly, on an order in
Office tick one small file and press **Translate by Claude**, then run the command
again: it should log the job, and the order page should show the Word draft within a
few minutes. Add `$env:KEEP_WORK = "1"` to keep the job's folder
(`C:\claude-worker\work\...`) for inspection, and delete it afterwards: it holds the
client's document.

## 4. Run it as a service

With [NSSM](https://nssm.cc) (the same tool the Office API uses), in an administrator
PowerShell:

```
nssm install ClaudeTranslateWorker C:\claude-worker\.venv\Scripts\python.exe C:\claude-worker\claude_worker.py
nssm set ClaudeTranslateWorker AppDirectory C:\claude-worker
nssm set ClaudeTranslateWorker AppEnvironmentExtra CLAUDE_WORKER_TOKEN=<the token> WORKER_NAME=claude-1
nssm set ClaudeTranslateWorker ObjectName .\<windows user> <that user's password>
nssm set ClaudeTranslateWorker AppStdout C:\claude-worker\worker.log
nssm set ClaudeTranslateWorker AppStderr C:\claude-worker\worker.log
nssm set ClaudeTranslateWorker AppRotateFiles 1
nssm start ClaudeTranslateWorker
```

`ObjectName` matters: Claude Code keeps its login in the Windows user's profile, so
the service must run as the user who signed in in step 1 (not LocalSystem).

The log has job ids, order numbers and statuses only, never document text.

## Settings (environment variables)

| Variable | Default | |
| --- | --- | --- |
| `CLAUDE_WORKER_TOKEN` | required | One of Office's `CLAUDE_WORKER_TOKENS` |
| `OFFICE_API_URL` | `https://api.suliko.ge` | |
| `WORKER_NAME` | computer name | Shown to support |
| `WORK_DIR` | `.\work` | Job folders, deleted when each job ends |
| `CLAUDE_BIN` | `claude` | Full path if it is not on PATH |
| `CLAUDE_MODEL` | Claude Code's default | e.g. `opus` |
| `POLL_SECONDS` | 15 | Wait when nothing is queued |
| `JOB_TIMEOUT_MINUTES` | 25 | Office puts a job back after 30, so keep it lower |
| `USAGE_LIMIT_PAUSE_MINUTES` | 30 | Pause after Claude's usage limit |
| `SOFFICE_BIN` | found automatically | LibreOffice's `soffice.exe`, for the page count |
| `RULES_FILE` | `translation-rules.md` beside the script | The bureau's standing rules, put in every job |

## The bureau's rules

`translation-rules.md` holds the bureau's standing rules (Georgian names, mirroring the
original, slashes for stamps and photos, `[-----]` for blank fields, no dashes,
passport layout, what to check before finishing). Every job's CLAUDE.md contains it,
ahead of the order's own instructions, which win where they disagree.

Edit the file on the server to change the rules: it is read for each job, so no
restart is needed. After Claude writes the translation the worker looks for an em
dash, an en dash or three hyphens in the text and, if it finds one, asks Claude once to
fix only those places.

## Landscape and page count

A table with 10 or more columns, or a translation Claude marks `<!--page: landscape-->`,
gets a landscape A4 page, narrow margins and columns sized to what they hold (not
split equally).

To make the Word file no longer than the original, install LibreOffice on this
machine (`winget install TheDocumentFoundation.LibreOffice`) and
`.\.venv\Scripts\python.exe -m pip install -r requirements.txt` again (it adds
`pypdf`). The worker then lays the file out, counts the pages and tightens the page in
steps: margins first, then type size. Ordinary text goes down to 10 pt, a wide table to
8 pt, never lower. The target is the original's page count (a PDF's pages; 1 for a
picture; Word and Excel sources are not fitted). If LibreOffice is not at
`C:\Program Files\LibreOffice`, set `SOFFICE_BIN` to its `soffice.exe`. Without it, a
wide table still gets a landscape page and one fixed size, but the page count is not
checked.

## What it does and does not do

- One job at a time. For more speed, run a second worker with another
  `WORKER_NAME` on the same or another machine: Office never hands one job twice.
- Claude Code is allowed to read, write and edit files in the job's folder and
  nothing else (no shell, no web).
- A usage limit puts the job back in the queue (it does not count as an attempt) and
  the worker pauses. Any other failure is shown to staff with a short message and a
  Retry button.
- Word and Excel sources are turned into text first; PDFs and images are read by
  Claude directly.
- `translation_docx.py` is a copy of `src/suliko/domain/translation_docx.py` in the API
  repository. When that file changes, copy it here again (a test fails until you do).
