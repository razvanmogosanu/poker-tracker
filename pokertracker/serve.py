"""A small local server so the report can refresh itself.

The dashboard is a static file, and a page opened over file:// cannot run the
parser. Serving it from localhost instead lets the page POST to /api/refresh
every 60 seconds, which runs exactly the same pipeline as `cli refresh` and
reports back how many new hands turned up.

Nothing here is exposed beyond the loopback interface and there is no state in
the server itself: every request opens its own SQLite connection, so the
threading server cannot trip over sqlite's per-thread rules.
"""

from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, derive, equity, ranges, report, solver

# Only one refresh may run at a time: two concurrent imports would fight over
# the same rows, and the pipeline is fast enough that queueing is fine.
_refresh_lock = threading.Lock()


def run_refresh(db_path: str, root: str, hero_hint: str = "",
                output: str = "") -> dict:
    """Import anything new, rebuild derived stats, compute EV. Returns a summary.

    With `output`, the on-disk report is rewritten too, so the file is not left
    describing the hands as they stood when the server was started.
    """
    started = time.time()
    with _refresh_lock:
        conn = db.connect(db_path)
        try:
            before = conn.execute("SELECT COUNT(*) n FROM hands").fetchone()["n"]
            paths = db.discover(root)
            res = db.import_paths(conn, paths, hero_hint=hero_hint)
            derive.rebuild(conn)
            ev_rows = equity.compute_all(conn)
            after = conn.execute("SELECT COUNT(*) n FROM hands").fetchone()["n"]
            problems = conn.execute("SELECT COUNT(*) n FROM problems").fetchone()["n"]
            # Only when the hands changed: the poll runs every 60 seconds and
            # most of them find nothing, which is no reason to render and write
            # the whole dashboard again. It is the static page that is written,
            # not the live one -- a file:// page still cannot poll anything.
            written = False
            if output and (after != before or not Path(output).exists()):
                who = hero_hint or db.detect_hero(conn)
                Path(output).write_text(report.build(conn, who), encoding="utf-8")
                written = True
            return {
                "ok": True,
                "new_hands": after - before,
                "total_hands": after,
                "files_read": res["files"],
                "files_skipped": res["skipped"],
                "ev_rows": ev_rows,
                "problems": problems,
                "report_written": written,
                "elapsed": round(time.time() - started, 1),
            }
        finally:
            conn.close()


def _range(spec: dict) -> dict:
    """Range text -> its cells and its canonical text, for the Solver grid.

    The page keeps no copy of the range syntax: it sends whatever was typed or
    clicked and paints what comes back, so the grid and the text box cannot
    come to disagree about what 'ATs+' means.
    """
    cells = ranges.parse(str(spec.get("range") or ""))
    return {
        "cells": sorted(cells, key=ranges.CELLS.index),
        "range": ranges.to_text(cells),
        "combos": int(len(ranges.combos(cells))),
    }


def make_handler(db_path: str, root: str, hero: str, output: str = ""):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(json.dumps(payload).encode("utf-8"), "application/json",
                       status)

        def _solver(self, work) -> None:
            """Run one Solver-tab request. A bad scenario is a 400 carrying the
            sentence the page shows, not a traceback in the terminal."""
            try:
                self._json(work())
            except (ValueError, KeyError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)

        def _hand(self) -> dict:
            query = parse_qs(urlparse(self.path).query)
            conn = db.connect(db_path)
            try:
                return solver.from_hand(
                    conn, hero or db.detect_hero(conn),
                    int(query["id"][0]), query.get("street", [""])[0])
            finally:
                conn.close()

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/api/hand":
                self._solver(self._hand)
                return
            if path not in ("/", "/index.html"):
                self._send(b"not found", "text/plain; charset=utf-8", 404)
                return
            conn = db.connect(db_path)
            try:
                who = hero or db.detect_hero(conn)
                html = report.build(conn, who, live=True)
            finally:
                conn.close()
            self._send(html.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):  # noqa: N802
            path = self.path.split("?")[0]
            # Neither Solver endpoint takes the refresh lock: they compute from
            # the request alone and write nothing.
            if path in ("/api/solve", "/api/range"):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if path == "/api/solve":
                    self._solver(lambda: solver.analyse(json.loads(body or b"{}")))
                else:
                    self._solver(lambda: _range(json.loads(body or b"{}")))
                return
            if path != "/api/refresh":
                self._send(b"not found", "text/plain; charset=utf-8", 404)
                return
            try:
                result = run_refresh(db_path, root, hero, output)
            except Exception as exc:  # surfaced in the page, not swallowed
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send(json.dumps(result).encode("utf-8"), "application/json")

        def log_message(self, fmt, *args):
            if self.command == "POST" and self.path.startswith("/api/refresh"):
                print(f"  refresh at {self.log_date_time_string()}")

    return Handler


def _already_running(port: int) -> bool:
    """True if something is already listening on the loopback port."""
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def serve(db_path: str, root: str, hero: str = "", port: int = 8765,
          open_browser: bool = True, output: str = "") -> None:
    url = f"http://127.0.0.1:{port}/"
    # Double-clicking the launcher twice should show the dashboard, not crash
    # with "address already in use".
    if _already_running(port):
        print(f"tracker already running at {url} - opening it")
        if open_browser:
            webbrowser.open(url)
        return

    handler = make_handler(db_path, root, hero, output)
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"serving the dashboard at {url}")
        print(f"  database    {Path(db_path).resolve()}")
        print(f"  histories   {root}")
        if output:
            print(f"  report      {Path(output).resolve()}")
        print("  press Ctrl+C to stop")
        if open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
