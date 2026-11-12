# SPDX-License-Identifier: GPL-3.0-only
"""Settings: what belongs to this Mac, in settings.json in the app data folder, and the
budget's own two, Overspending and Usual's months, kept in the budget (its meta) so a
backup carries them (issue #153).

Every value is validated on load and on save, so a stale or hand-edited file can never
put the app into a bad state.

**An update never changes a setting a copy uses** (docs/adr/0007). A default is only for
a new copy (`DEFAULTS`) or a new budget (`BUDGET_DEFAULTS`). The first time a copy from
before 1.4.1 launches, its settings.json is written out whole with the values it was
using: a value it had stays, and one it had no value for keeps what it ran on (`RAN_ON`,
`BUDGET_RAN_ON`), never a newer default. 1.0.5 made This month the default and copies with
no Overspending saved went from Next month to This month without a word: that's what this
rule is for. Settings this version doesn't know (a later version's) are kept as they are,
so going back a version and forward again loses none.
"""

import json
import logging
import os
import threading
from pathlib import Path

from budget import OVERSPENDING

log = logging.getLogger("inab")

THEMES = ("system", "light", "dark")
DASHBOARD_MONTHS = (3, 6, 12, "all")
USUAL_MONTHS = (3, 6, 12)

# This Mac's settings, as a new copy starts them.
DEFAULTS = {
    "theme": "system",
    "auto_import": True,          # import bank files that land in Downloads
    "downloads_dir": "",          # '' means ~/Downloads
    "update_automatically": True,
    "dashboard_months": 6,        # the Dashboard's months: one of DASHBOARD_MONTHS
}

# What a copy ran on for a setting its settings.json had no value for (one added after it
# was made). Never change a value here, whatever happens to DEFAULTS: a copy that skips
# versions still has to keep what it was using. A new setting goes here with the value
# that keeps what copies did before it existed.
RAN_ON = {
    "theme": "system",
    "auto_import": True,
    "downloads_dir": "",
    "update_automatically": True,
    "dashboard_months": 6,
}

# The budget's own settings (in its meta), as a new budget starts them. Next month is the
# default since 1.4.1 (issue #166): with This month, assigning to a category already
# overspent in cash left Ready to assign as it was, since the money had already come out
# of it, and that read as broken. Next month was the rule before 1.0.5. Don't switch it
# back without asking the maintainer.
BUDGET_DEFAULTS = {
    "overspending": "next_month",  # how overspending is paid for: one of OVERSPENDING (see budget.py)
    "average_months": 3,           # how many whole months a category's Usual looks back over
}

# What a budget from before 1.4.1 ran on when settings.json had no value: This month was the
# default from 1.0.5 to 1.4.0. Never change these.
BUDGET_RAN_ON = {"overspending": "this_month", "average_months": 3}

# A copy from before 1.4.1 keeps the two at settings.json's top level, as they were when they
# moved into the budget, and never changes them after: 1.4.0 reads them there, and a backup
# from before 1.4.1 restored later takes them. A copy made since has neither.
_OLD_KEYS = ("overspending", "carry_overspending", "average_months")  # 1.0.3's switch is written out as Overspending


def validate(raw, missing=DEFAULTS):
    """This Mac's settings, complete and valid, from `raw` (unknown keys dropped): one without
    a valid value there takes `missing`'s."""
    s = dict(missing)
    if not isinstance(raw, dict):
        return s
    if raw.get("theme") in THEMES:
        s["theme"] = raw["theme"]
    for key in ("auto_import", "update_automatically"):
        if isinstance(raw.get(key), bool):
            s[key] = raw[key]
    if isinstance(raw.get("downloads_dir"), str):
        s["downloads_dir"] = raw["downloads_dir"].strip()
    if raw.get("dashboard_months") in DASHBOARD_MONTHS and not isinstance(raw["dashboard_months"], bool):
        s["dashboard_months"] = raw["dashboard_months"]
    return s


def validate_budget(raw, missing):
    """The budget's own settings, complete and valid, from `raw`: one without a valid value
    there takes `missing`'s. The on/off switch Overspending replaced (1.0.3, 1.0.4) on means
    Carry over."""
    s = dict(missing)
    if not isinstance(raw, dict):
        return s
    if raw.get("overspending") in OVERSPENDING:
        s["overspending"] = raw["overspending"]
    elif raw.get("carry_overspending") is True:
        s["overspending"] = "carry_over"
    if type(raw.get("average_months")) is int and raw["average_months"] in USUAL_MONTHS:
        s["average_months"] = raw["average_months"]
    return s


class Settings:
    """This Mac's settings (thread-safe). `on_change` listeners get the new dict."""

    def __init__(self, path):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._listeners = []
        self._before = None  # what a budget from before 1.4.1 ran on, on this Mac (None: none did)
        self._others = {}    # settings this version doesn't know (a later version's), kept as they are
        self._values = self._load()

    def _load(self):
        """A new copy's settings start as DEFAULTS. A copy from before 1.4.1 writes out every
        setting with the value it was using, the two that moved into the budget included."""
        if not self._path.exists():
            values = dict(DEFAULTS)
            self._write(values)
            return values
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("Could not read %s; using what a copy without settings ran on", self._path)
            self._before = dict(BUDGET_RAN_ON)
            return dict(RAN_ON)
        raw = raw if isinstance(raw, dict) else {}
        values = validate(raw, RAN_ON)
        if any(key in raw for key in _OLD_KEYS):
            self._before = validate_budget(raw, BUDGET_RAN_ON)
        self._others = {k: v for k, v in raw.items() if k not in DEFAULTS and k not in _OLD_KEYS}
        if raw != self._file(values):
            self._write(values)
            log.info("Wrote out the settings as this copy had them")
        return values

    def _file(self, values):
        return {**self._others, **values, **(self._before or {})}

    def _write(self, values):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._file(values), indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def get(self):
        with self._lock:
            return dict(self._values)

    def before(self):
        """What a budget from before 1.4.1 ran on, on this Mac: the Overspending and Usual's
        months its settings.json had, or what a copy without them ran on."""
        with self._lock:
            return dict(self._before or BUDGET_RAN_ON)

    def update(self, changes):
        with self._lock:
            merged = validate({**self._values, **(changes if isinstance(changes, dict) else {})}, self._values)
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


# ── the budget's own ──

def budget_settings(conn):
    """Overspending and Usual's months, from the budget's meta. A budget from before 1.4.1 that
    hasn't moved its own in yet (move_into_budget, at launch) ran on BUDGET_RAN_ON."""
    stored = {r["key"]: r["value"] for r in conn.execute(
        f"SELECT key, value FROM meta WHERE key IN ({','.join('?' * len(BUDGET_DEFAULTS))})", tuple(BUDGET_DEFAULTS))}
    months = stored.get("average_months")
    return validate_budget({"overspending": stored.get("overspending"),
                            "average_months": int(months) if months and months.isdigit() else None}, BUDGET_RAN_ON)


def save_budget_settings(conn, changes):
    """Changes the budget's own settings (a value that isn't valid changes nothing). Inside a
    db.write(keep_undo=True): no undo step, and ⌘Z keeps working (no step's SQL touches them)."""
    current = budget_settings(conn)
    for key, value in validate_budget({**current, **changes}, current).items():
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, str(value)))


def new_budget(conn):
    """A budget just made gets BUDGET_DEFAULTS (db.py, as it creates the database)."""
    for key, value in BUDGET_DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def move_into_budget(db, store):
    """Once, at launch, for a budget from before 1.4.1 (one this version or a later one made has
    them from the start, `new_budget`): Overspending and Usual's months move into it as this
    Mac had them, or as a copy without them ran (`store.before()`). That's also what a backup
    from before 1.4.1 gets when it's restored. Not an undo step, and it leaves ⌘Z as it is."""
    with db.write(keep_undo=True) as c:
        have = {r["key"] for r in c.execute(
            f"SELECT key FROM meta WHERE key IN ({','.join('?' * len(BUDGET_DEFAULTS))})", tuple(BUDGET_DEFAULTS))}
        moved = {key: value for key, value in store.before().items() if key not in have}
        for key, value in moved.items():
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
    if moved:
        log.info("Moved into the budget from settings.json: %s",
                 ", ".join(f"{key} {value}" for key, value in moved.items()))
