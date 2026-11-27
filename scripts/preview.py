#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Development preview: the app's page in an ordinary browser, driving the real engine.

    python3 scripts/preview.py [--data FOLDER] [--port 8765] [--today 2026-10-12] [--platform windows]
                               [--computer PC-B]

Serves Application Files/index.html on http://127.0.0.1:<port> with a stand-in for
pywebview's bridge that POSTs to /api, which calls the same BudgetService.dispatch()
the app uses. Handy for looking at and clicking through the interface (Claude's
browser pane can't see inside the app's own window). `--platform` pretends to be the
other system: the page's wording and keys follow it (issue #185); the fonts are this
computer's.

The budget folder (issue #189) works as in the app: a look at launch, the watcher every few
seconds (the page hears of it by asking /events), and a save at quit. Two previews on two
ports, each with its own --data and --computer name, sharing one budget folder, are two
computers. There's no folder dialog: Choose… answers with the folder named on the first line of
`<data>/choose-folder.txt`, every time until it's changed (none there: nothing chosen). The
folder suggested is `<data> budget folder`, beside the data folder, as with INAB_DATA_DIR.
Quitting (the page's) saves as the app does, then stops the preview: start it again to reopen.

This is a development tool only: it isn't bundled, and the app itself never opens a
network port. It refuses to use the app's real data folder; by default it uses
~/Library/Caches/I Need A Budget-preview.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = ROOT / "Application Files"
sys.path.insert(0, str(APP_FILES))
import computer  # noqa: E402

REAL_DATA = computer.this().data_dir()
WATCH_EVERY = 3  # seconds between the budget folder watcher's looks (the app's: 4)
SHIM = ("<script>(() => {"
        "const post = (path, body) => fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, "
        "body: JSON.stringify(body || {})}).then(r => r.json());"
        "window.pywebview = {api: {"
        "call: (name, args) => post('/api', {name, args}),"
        "log_error: m => console.error(m),"
        "choose_folder: start => post('/choose-folder', {start}),"
        "look_before_quit: () => post('/look-before-quit'),"
        "quit: () => post('/quit').then(() => { document.body.innerHTML = "
        "'<p style=\"padding:40px\">Quit. Start the preview again to open the app again.</p>'; }),"
        "asking_before_quit: () => console.log('Quit waits')}};"
        # main.py's events (window.appEvent), asked for every second
        "const events = setInterval(() => fetch('/events').then(r => r.json()).then(list => list.forEach(([n, p]) => "
        "window.appEvent && window.appEvent(n, p))).catch(() => clearInterval(events)), 1000);"
        "})();</script>\n")


def prepare(data):
    """The preview's settings, with a Downloads folder that isn't the real one: when the data folder's
    settings.json has none, `<data>/downloads` (created empty); refuses the real Downloads. Onboarding
    imports the bank files in that folder and moves them into the preview's Imported Files/."""
    from settings import Settings

    data = Path(data)
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings(data / "settings.json")
    if not settings.get()["downloads_dir"]:
        settings.update({"downloads_dir": str(data / "downloads")})
    downloads = settings.downloads_dir().expanduser()
    if downloads.resolve() in {computer.this().downloads_dir().resolve(), (Path.home() / "Downloads").resolve()}:
        sys.exit("The preview never uses your real Downloads folder: onboarding would move the bank files in it. "
                 "Set downloads_dir in the preview's settings.json to a folder of copies.")
    downloads.mkdir(parents=True, exist_ok=True)
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=str(Path.home() / "Library" / "Caches" / "I Need A Budget-preview"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--today", help="pretend it's this date (YYYY-MM-DD)")
    parser.add_argument("--platform", choices=("mac", "windows"), default=computer.this().name,
                        help="the system the page thinks it's on (default: this one)")
    parser.add_argument("--computer", help="this computer's name in the budget folder (default: the system's)")
    args = parser.parse_args()
    data = Path(args.data).expanduser().resolve()
    if data == REAL_DATA.resolve():
        sys.exit("The preview never uses the app's real data folder. Pass a throwaway --data folder.")
    if args.today:
        os.environ["INAB_TODAY"] = args.today
    if args.computer:
        os.environ["INAB_COMPUTER"] = args.computer
    settings = prepare(data)

    import budget_folder
    from db import Database
    from service import BudgetService

    name = budget_folder.this_computer()
    folder = settings.get()["budget_folder"]
    tag = budget_folder.computer_tag(name) if folder else None
    service = BudgetService(Database(data / "budget.db", backup_tag=tag), settings, data, computer_name=name, hooks={
        "app_info": lambda: {"version": "preview", "data_dir": str(data), "log_path": "", "updates": None,
                             "platform": args.platform, "computer": name,
                             "suggested_folder": str(data.with_name(f"{data.name} budget folder"))},
        "open_license": lambda: subprocess.run(["open", "-t", str(ROOT / "LICENSE")], check=True) and True,
    })
    service.budget_folder_look(at_launch=True)  # as main.py does, before anything else
    service.read_imported_files()  # as the app does at launch, after an upgrade (issue #148)
    service.read_currency()  # as the app does at the first launch of 1.4.3 (issue #76)
    events, watching = [], threading.Event()
    watching.set()

    def watch():
        """main.py's budget folder watcher: what it would send the page waits in `events`."""
        shown = None
        while watching.is_set():
            try:
                state = service.budget_folder_look()
            except Exception as e:  # noqa: BLE001 - as the app, it carries on
                print(f"Watching the budget folder failed: {e!r}", flush=True)
                state = None
            now = state and {k: v for k, v in state.items() if k != "took"}
            if state and watching.is_set() and (state.get("took") or now != shown):
                shown = now
                events.append(["budget_folder", state])
            time.sleep(WATCH_EVERY)

    def chosen():
        try:
            lines = (data / "choose-folder.txt").read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if not lines or not lines[0].strip():
            print(f"Choose… chose nothing: put a folder on the first line of {data / 'choose-folder.txt'}", flush=True)
            return None
        return lines[0].strip()

    def quit_now(server):
        watching.clear()
        service.budget_folder_at_quit()
        print("Quit", flush=True)
        threading.Thread(target=server.shutdown, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/events":
                sent = events[:]
                del events[:len(sent)]
                return self._send(200, json.dumps(sent), "application/json")
            if self.path not in ("/", "/index.html"):
                return self._send(404, "Not found", "text/plain")
            html = (APP_FILES / "index.html").read_text(encoding="utf-8")
            html = html.replace("connect-src 'none'", "connect-src 'self'")
            html = html.replace("<script>\n'use strict';", SHIM + "<script>\n'use strict';", 1)
            self._send(200, html, "text/html; charset=utf-8")

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api":
                result = service.dispatch(str(request.get("name")), request.get("args"))
            elif self.path == "/choose-folder":
                result = chosen()
            elif self.path == "/look-before-quit":
                result = service.budget_folder_look(now=True)
            elif self.path == "/quit":
                result = quit_now(self.server)
            else:
                return self._send(404, "Not found", "text/plain")
            self._send(200, json.dumps(result), "application/json")

        def log_message(self, *args):
            pass

    print(f"Preview on http://127.0.0.1:{args.port} (data: {data}, as on {args.platform}, "
          f"this computer: {name})", flush=True)
    threading.Thread(target=watch, name="budget-folder-watcher", daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
