"""User settings, stored as JSON in the app data folder.

Every value is validated on load and on save, so a stale or hand-edited file can
never put the app into a bad state.
"""

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("inab")

THEMES = ("system", "light", "dark")

DEFAULTS = {
    "theme": "system",
    "auto_import": True,          # import Tangerine files that land in Downloads
    "downloads_dir": "",          # '' means ~/Downloads
    "update_automatically": True,
    "average_months": 3,          # how many months "needed per month" looks back over
}


def validate(raw):
    """Return a complete, valid settings dict built from `raw` (unknown keys dropped)."""
    s = dict(DEFAULTS)
    if not isinstance(raw, dict):
        return s
    if raw.get("theme") in THEMES:
        s["theme"] = raw["theme"]
    for key in ("auto_import", "update_automatically"):
        if isinstance(raw.get(key), bool):
            s[key] = raw[key]
    if isinstance(raw.get("downloads_dir"), str):
        s["downloads_dir"] = raw["downloads_dir"].strip()
    if raw.get("average_months") in (3, 6, 12):
        s["average_months"] = raw["average_months"]
    return s


class Settings:
    """Thread-safe settings store. `on_change` listeners get the new dict."""

    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._listeners = []
        self._values = self._load()

    def _load(self):
        if self._path.exists():
            try:
                return validate(json.loads(self._path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.exception("Could not read %s; using defaults", self._path)
                return dict(DEFAULTS)
        values = dict(DEFAULTS)
        self._write(values)
        return values

    def _write(self, values):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(values, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self):
        with self._lock:
            return dict(self._values)

    def update(self, changes):
        with self._lock:
            merged = validate({**self._values, **(changes if isinstance(changes, dict) else {})})
            changed = merged != self._values
            if changed:
                self._values = merged
                self._write(merged)
            values = dict(self._values)
        if changed:
            for listener in list(self._listeners):
                try:
                    listener(values)
                except Exception:
                    log.exception("Settings listener failed")
        return values

    def on_change(self, listener):
        self._listeners.append(listener)

    def downloads_dir(self):
        custom = self.get()["downloads_dir"]
        return Path(custom).expanduser() if custom else Path.home() / "Downloads"
