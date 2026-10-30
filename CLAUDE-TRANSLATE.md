# Claude Translate: the worker's side

A person on an order ticks source files and clicks **Translate by Claude**. Each
ticked file becomes a queued job. A separate server running Claude Code (the
worker) asks this API for jobs, translates each file to a Word document and
sends it back. Nothing is translated when a file is merely uploaded, and the
worker only ever sees files someone picked.

This file is the contract between Suliko Office and the worker. A ready-made worker for
a Windows server is in `worker/` (see `worker/README.md`).

## What Office does

- A superadmin switches **Claude Translate** on per organisation (tenant page in
  the admin area). Off by default. A bureau and a freelancer's personal account
  are both organisations.
- Staff of a switched-on organisation pick files in a dialog. Files already
  waiting are refused; files that already have a Claude translation need an
  extra confirmation (the old one is kept, the new one is added beside it).
- The result is filed under the same document's **Translation** files, labelled
  Claude, and opens in the side-by-side Word editor. It is a draft for the
  translator, never a finished or certified document. The order's own status is
  never changed.
- The order's **Translation instructions** field (names, glossary, rules) travels
  with every job.

## Setup on the Office API server

In the server's `.env`, then restart `SulikoAPI`:

```
# One or more tokens, comma separated. Add the new one, switch the worker over,
# then remove the old one: that is how a token is rotated. Empty = worker API off.
CLAUDE_WORKER_TOKENS=<long random string>
# Optional: only these addresses (or CIDR ranges) may call. The address is the one
# the reverse proxy saw (last X-Forwarded-For hop).
CLAUDE_WORKER_ALLOWED_IPS=203.0.113.7
# Optional tuning (defaults shown)
CLAUDE_JOB_STUCK_MINUTES=30
CLAUDE_JOB_MAX_ATTEMPTS=3
CLAUDE_WORKER_OFFLINE_SECONDS=300
```

Generate a token with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Run `alembic upgrade head` (revision 0038) before the first start.

The worker API does not need the gateway secret (`BFF_SHARED_SECRET`): the worker
is not the frontend. The bearer token is what is checked. A wrong or missing
token, or a refused address, is a plain `401`.

## Endpoints

Base: `https://api.suliko.ge/api/v1/worker`. Every call carries
`Authorization: Bearer <token>`. Optionally `X-Worker-Name: <name>` to tell
several workers apart on the admin page.

### `POST /jobs/claim`

Returns the oldest waiting job of any switched-on organisation and makes it
yours (status `translating`, attempts + 1). `204` with no body when nothing waits.
Atomic: two workers calling at once never get the same job. Every call is also a
heartbeat.

```json
{
  "job_id": "12.k3Jd9...",
  "order_number": 1436,
  "file_name": "ASUS_ISO14001.pdf",
  "content_type": "application/pdf",
  "size_bytes": 482113,
  "source_language": "ru",
  "target_language": "ka",
  "source_language_name": "Russian",
  "target_language_name": "Georgian",
  "instructions": "free text from the order, or null",
  "attempts": 1,
  "output_file_name": "ASUS_ISO14001_Translated.docx",
  "source_path": "/api/v1/worker/jobs/12.k3Jd9.../source"
}
```

`job_id` is `<organisation id>.<public id>`: use it as given.

### `GET /jobs/{job_id}/source`

The file to translate (`Content-Disposition: attachment`). Only while the job is
yours: `409` otherwise, `404` if the file was removed.

### `POST /jobs/{job_id}/result`

`multipart/form-data` with one field `file`: the `.docx`. Office checks it is a
Word document without macros, files it as
`<name>_Translated.docx` under the document (with ` (2)`, ` (3)` if that name is
taken: nothing is replaced) and marks the job `translated`, in one step.

```json
{ "status": "translated", "output_file_id": "…", "output_file_name": "ASUS_ISO14001_Translated.docx" }
```

`409` if the job is not `translating` (already done, cancelled, or put back
because it took too long). `422` if the bytes are not a Word document.

### `POST /jobs/{job_id}/status`

For a job that did not come to a result:

```json
{ "status": "failed", "error_message": "file is encrypted" }
{ "status": "queued", "error_message": "usage limit, retry later" }
```

`failed`: the person sees the message and may press Retry. `queued`: try again
later (a usage limit, say); the attempt it used is given back, so this does not
count against the three. A success is never reported here: send the result.
`error_message` is shown to staff, so keep it short and free of file contents.

### `POST /heartbeat`

`204`. Optional body `{ "detail": "worker 1.4" }`. Call it about once a minute
while idle; `claim` already counts. The admin tenant page shows the last time and
whether the server looks online (silent for five minutes = offline).

## Rules the worker should follow

1. Loop: `claim` → (204: sleep 10-30 s) → `source` → translate → `result`. On a
   failure, `status` with `failed`, so the person sees why.
2. Output is always `.docx`. Use `output_file_name` as given.
3. Put `instructions` in the CLAUDE.md of the working folder, with the languages.
4. Never log file contents. Job ids, order numbers, status changes and errors only.
5. A job held longer than 30 minutes without a result is put back in the queue by
   Office by itself (attempts + 1); after 3 attempts it fails with a message.
   Finish or report within that time.
6. If the usage limit is reached, report `queued` with a message and stop claiming
   for a while.

## Quick test with curl

```
curl -s -X POST -H "Authorization: Bearer $TOKEN" https://api.suliko.ge/api/v1/worker/jobs/claim
curl -s -H "Authorization: Bearer $TOKEN" -o in.pdf "https://api.suliko.ge/api/v1/worker/jobs/$JOB/source"
curl -s -H "Authorization: Bearer $TOKEN" -F "file=@out.docx" "https://api.suliko.ge/api/v1/worker/jobs/$JOB/result"
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"failed","error_message":"short reason"}' "https://api.suliko.ge/api/v1/worker/jobs/$JOB/status"
```
