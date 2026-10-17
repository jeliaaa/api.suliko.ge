"""Local receiver for the tarjimnebi extractor.

    python scripts/tarjimnebi/receive.py [out_dir]

Listens on 127.0.0.1:8765 only. The extractor (extract.js, run inside the
signed-in app.tarjimnebi.ge page) POSTs JSON to /save?name=<file>; each call
overwrites out_dir/<file>.json. No credentials pass through here: the browser
session stays in the browser and the extractor only issues GET requests.
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "tarjimnebi-dump")
OUT.mkdir(parents=True, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "https://app.tarjimnebi.ge")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        """Serve extract.js so it can be loaded into the page without pasting."""
        if urlparse(self.path).path != "/extract.js":
            self.send_response(404)
            self.end_headers()
            return
        body = (Path(__file__).with_name("extract.js")).read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/javascript; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
        if not re.fullmatch(r"[a-z0-9_\-]{1,60}", name):
            self.send_response(400)
            self._cors()
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        data = json.loads(body)
        (OUT / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self.send_response(200)
        self._cors()
        self.end_headers()
        self.wfile.write(b"ok")
        print(f"saved {name}.json", flush=True)

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    print(f"listening on 127.0.0.1:8765, writing to {OUT.resolve()}")
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
