# SPDX-License-Identifier: GPL-3.0-only
"""The budget folder (docs/adr/0008): the folder you choose for your budget, on any computer.
Whatever syncs or backs it up (OneDrive, iCloud Drive, Dropbox, a drive of your own) takes the
budget to your other computers: the app never goes online for it.

What's where:
- This computer's data folder keeps the working copy (`budget.db`, written as you work: SQLite
  in WAL mode, three files, never in the budget folder: a sync tool could catch it halfway), and
  `Saves/`: a copy of the save it started from, and the few before it (`<save id>.inab`), so the
  window can show what each computer changed. Its settings and logs are there too.
- The budget folder keeps `Budget.inab` (the budget, whole), `Backups/` (every computer's,
  backups.py's rules) and `Imported Files/`, and a note from each computer the budget is open on
  (`.Budget open on <computer>.json`), removed at quit: a heads-up, not a lock.

**Saving** (`_save`): a complete copy of the working copy (`VACUUM INTO`, in Saves/) with what's
kept about the save written into it (meta `save`: an id, the saves it was built on, which
computer saved it, when, the app's version and the budget format's), copied into the folder
under a hidden name and renamed over Budget.inab, tried again while Windows or a sync tool
briefly holds the file (`_replace`). Only when the budget changed since the save it started
from (`backups.digest`, which leaves out what's kept about saves and backups: they aren't a
change), a few seconds after changes stop (`SAVE_AFTER`), and at quit.

**Watching** (`check`, every few seconds, the Downloads watcher's rhythm): every budget in the
folder is a save: Budget.inab, and a sync tool's conflict copy (OneDrive's
`Budget-<computer>.inab`, iCloud's `Budget 2.inab`, Dropbox's
`Budget (… conflicted copy …).inab`) the same way. Its history says how it stands to this
computer's (`relation`):
- this computer's, or one it was built on: nothing new (a conflict copy goes into Backups/);
- built on this computer's: taken straight away when this computer has changed nothing since
  the save it started from (the working copy becomes it, and ⌘Z is cleared, as an import does);
  otherwise the budget was **changed on two computers**;
- neither (each computer saved on top of the save they both started from, before seeing the
  other's): changed on two computers.
Changed on two computers, nothing is saved or taken until you choose: `two_computers()` gives the
three budgets the window shows each side's changes from (the save both started from, this
computer's, the other's) as files, and `keep_this` / `keep_other` decide. The one not kept goes
into Backups/ (`budget-not-kept-<date and time>.db`), and the folder then holds the one kept
under a save whose history has both sides, so each computer sees it was built on its own and
takes it.

A budget saved by a newer version of the budget's format is never taken, nor saved over
(`newer`): this copy needs the update. An older one is upgraded as at launch, its
before-upgrade copy in Backups/; the upgrade itself isn't a change to save.

**A folder that can't be reached** (an unplugged drive, a sync tool not running): the app works
on this computer's copy, backups wait in the data folder's `Backups/` and imported files in its
`Imported Files/`, and both move over when the folder is back, which `check` keeps looking for.
The budget folder itself is never made again by the app (only `move_to` makes one): a drive's
folder must not be made on the disk it's mounted from.

**Moving in** (`move_to`: the 1.5.0 upgrade, and Settings' Move… later): the budget is saved
there, the backups and imported files follow, and an old budget folder keeps a note that it
moved (`.Budget moved.json`), so a computer still using it doesn't put the budget back. A folder
that already has a budget is never written to. **Opening** one (`open_from`, a new computer):
this computer's budget, unless it's new and empty, goes into that folder's Backups/ first.

Locks: this module's, then the database's, never the other way round (the page's writes only
take the database's).
"""

import errno
import filecmp
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import backups
from db import SCHEMA_VERSION
from errors import UserError
from util import short_day

log = logging.getLogger("inab")

FILE_NAME = "Budget.inab"
EXTENSION = ".inab"
BACKUPS = "Backups"
IMPORTED_FILES = "Imported Files"
SAVES = "Saves"               # in the data folder
KEPT_SAVES = 5                # besides the one this computer started from
HISTORY_KEPT = 50             # how many saves a save remembers it was built on
SAVE_AFTER = 5                # seconds without a change before saving
REPLACE_TRIES = 40            # × REPLACE_WAIT: how long a file Windows or a sync tool holds is waited for
REPLACE_WAIT = 0.25
SAVE_KEY = backups.SAVE_KEY   # meta: the save this budget is, or started from
MOVED_NOTE = ".Budget moved.json"
THIS_COMPUTER = "This computer.inab"  # in Saves/: this computer's budget, for two_computers()
_OPEN_NOTE = re.compile(r"\.Budget open on (.+)\.json")

# How a save in the folder stands to this computer's (`relation`).
SAME, OLDER, NEWER, APART = "same", "older", "newer", "apart"


class NotABudget(Exception):
    """A file that isn't a whole budget: half synced, or something else with the extension."""


@dataclass
class Save:
    id: str
    history: list = field(default_factory=list)  # its own id first, then the saves it was built on, newest first
    computer: str = None
    saved_at: str = None        # ISO, with the saving computer's offset from UTC
    app_version: str = None
    schema_version: int = SCHEMA_VERSION
    digest: str = None          # backups.digest of the budget as saved
    name: str = None            # its name in the budget folder: Budget.inab, or a conflict copy's
    path: Path = None           # its copy in Saves/

    def kept(self):
        """What's kept in meta about it."""
        return json.dumps({"id": self.id, "history": self.history, "computer": self.computer,
                           "saved_at": self.saved_at, "app_version": self.app_version,
                           "schema_version": self.schema_version, "digest": self.digest})

    @classmethod
    def from_kept(cls, text):
        raw = json.loads(text)
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise ValueError("not a save")
        history = [h for h in raw.get("history") or [] if isinstance(h, str)]
        return cls(id=raw["id"], history=history or [raw["id"]], computer=raw.get("computer"),
                   saved_at=raw.get("saved_at"), app_version=raw.get("app_version"),
                   schema_version=int(raw.get("schema_version") or SCHEMA_VERSION), digest=raw.get("digest"))

    def about(self):
        """Who saved it and when, for the window."""
        return {"computer": self.computer, "saved_at": self.saved_at, "app_version": self.app_version}


def this_computer():
    """This computer's name as the window shows it ("Saved on …"): INAB_COMPUTER when it's set
    (the tests, two dev previews sharing one folder), else the name the system has for it."""
    name = os.environ.get("INAB_COMPUTER") or platform.node() or "This computer"
    return name[:-len(".local")] if name.endswith(".local") else name


def relation(save, mine):
    """How `save`, in the folder, stands to `mine`, the save this computer's budget is or started
    from: SAME, OLDER (one this computer's was built on), NEWER (built on this computer's; a file
    without a history, a backup copied in by hand, counts as that) or APART (neither)."""
    if mine is None:
        return APART
    if save.id == mine.id:
        return SAME
    if save.id in mine.history:
        return OLDER
    if mine.id in save.history or not save.history:
        return NEWER
    return APART


def read_save(path):
    """What's kept about the save in the budget file at `path`, a Save (without `name` or `path`).
    A file without it (a backup copied in by hand) gets an id from its contents and no history.
    NotABudget for a file that isn't a whole budget."""
    try:
        with closing(sqlite3.connect(str(path))) as c:
            if c.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise NotABudget("damaged")
            rows = dict(c.execute("SELECT key, value FROM meta WHERE key IN ('schema_version', ?)", (SAVE_KEY,)))
        schema_version = int(rows["schema_version"])
    except (sqlite3.Error, KeyError, ValueError, TypeError) as e:
        raise NotABudget(str(e) or type(e).__name__) from None
    if rows.get(SAVE_KEY):
        try:
            save = Save.from_kept(rows[SAVE_KEY])
        except (ValueError, TypeError):
            save = None
        if save is not None:
            save.schema_version = schema_version  # the file's own: what decides whether it can be opened
            return save
    return Save(id="file-" + _sha256(path)[:24], history=[], schema_version=schema_version)


def newer_message(save):
    """Why a budget saved by a newer version can't be opened here."""
    by = f"I Need A Budget {save.app_version}" if save.app_version else "a newer version of I Need A Budget"
    on = f" on {save.computer}" if save.computer else ""
    return f"This budget was saved by {by}{on}. Update this copy to open it."


def has_a_budget_message(save):
    if save is None or not save.computer:
        return "This folder already has a budget."
    return f"This folder already has a budget, saved by {save.computer} on {when(save.saved_at)}."


def when(stamp):
    """'2026-11-27T09:14:05-07:00' → 'Nov 27, 2026 at 9:14 AM'."""
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return "an unknown date"
    hour = moment.hour % 12 or 12
    noon = "AM" if moment.hour < 12 else "PM"
    return f"{short_day(moment.date().isoformat())}, {moment.year} at {hour}:{moment.minute:02d} {noon}"


class BudgetFolder:
    """The budget folder of one computer: `db` is its working copy, `data_dir` its data folder
    (Imported Files waits there while the folder can't be reached), `folder` the budget folder
    (None until one is chosen). `computer` names this computer in saves and notes; `clock`,
    `now` and `sleep` are for the tests."""

    def __init__(self, db, data_dir, folder=None, computer=None, app_version="dev", save_after=SAVE_AFTER,
                 clock=time.monotonic, now=None, sleep=time.sleep):
        self.db = db
        self.data_dir = Path(data_dir)
        self.computer = computer or this_computer()
        self.app_version = app_version
        self._save_after = save_after
        self._clock = clock
        self._now = now or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._lock = threading.RLock()
        self._seen = {}            # path in the folder → ((mtime, size), Save): read already
        self._other = None         # changed on two computers: the other computer's save
        self._mark = None          # db.conn.total_changes when the digest was last worked out
        self._digest = None
        self._last_change = None   # clock() when a change was last seen
        self._reachable = None     # whether the folder could be reached at the last look
        self._noted = None         # the folder this computer's note is in
        self._since = None         # when the budget was opened here, for the note
        self._problem = None       # the last problem logged, so a round that fails again isn't logged again
        self._state = None
        self.folder = None
        if folder:
            self._use(Path(folder))

    # ── the state, for the window ──

    def state(self):
        """What the window shows about the budget folder: {'folder', 'state', 'unsaved', 'saved', ...}.
        'state' is one of: none (no budget folder yet), unreachable (working on this computer's copy),
        waiting (Budget.inab can't be read yet: still syncing), saved, unsaved (changes not saved
        yet), two_computers ('other': who saved the other one, when), newer ('other': saved by a
        newer version), moved ('moved': which computer moved it, when). 'saved': the save this
        computer's budget is or started from; 'open_elsewhere': the other computers it's open on."""
        state = self._state
        return dict(state) if state else self.check(wait=True)

    def _set(self, kind, **extra):
        mine = self._mine()
        state = {"folder": str(self.folder) if self.folder else None, "state": kind,
                 "unsaved": self.folder is not None and self._changed(),
                 "saved": mine.about() if mine else None, **extra}
        if kind not in ("none", "unreachable"):
            state["open_elsewhere"] = self.others_open()
        self._state = state
        return dict(state)

    # ── watching ──

    def check(self, wait=True):
        """One look at the folder: the watcher's every few seconds, and at launch and quit
        without `wait`ing for changes to stop. Takes another computer's newer save, saves this
        computer's budget, or finds it changed on two computers. Returns the state, with 'took'
        (who saved it, when) when it took another computer's save."""
        with self._lock:
            folder = self.folder
            if folder is None:
                return self._set("none")
            if not _is_dir(folder):
                if self._reachable is not False:
                    log.warning("The budget folder %s can't be reached: working on this computer's copy", folder)
                self._reachable = False
                self._noted = None
                self._point_backups()
                return self._set("unreachable")
            if self._reachable is not True:
                if self._reachable is False:
                    log.info("The budget folder %s is back", folder)
                self._reachable = True
                self._point_backups()
                self._bring_leftovers(folder)
            try:
                state = self._look(folder, wait)
                self._problem = None
                return state
            except (OSError, sqlite3.Error) as e:
                if str(e) != self._problem:
                    log.warning("Couldn't use the budget folder %s: %s", folder, e)
                    self._problem = str(e)
                return self._set("waiting")

    def _look(self, folder, wait):
        self._note_open(folder)
        saves, unreadable = self._read_folder(folder)
        main = next((s for s in saves if s.name == FILE_NAME), None)
        if main is None and (FILE_NAME in unreadable or (folder / f".{FILE_NAME}.icloud").exists()):
            return self._set("waiting")  # still syncing, or iCloud hasn't downloaded it
        if not saves and (moved := _read_json(folder / MOVED_NOTE)):
            return self._set("moved", moved={"computer": moved.get("computer"), "moved_at": moved.get("moved_at")})
        newer = next((s for s in saves if s.schema_version > SCHEMA_VERSION), None)
        if newer is not None:
            self._other = None
            return self._set("newer", other=newer.about(), message=newer_message(newer))
        mine = self._mine()
        others = []
        for save in saves:  # Budget.inab first
            if relation(save, mine) in (SAME, OLDER):
                if save.name != FILE_NAME:
                    self._put_away(folder, save, "a sync tool's copy of a save this computer has")
            else:
                others.append(save)
        if others:
            other = others[0]
            if relation(other, mine) == NEWER and self._take(other, only_if_unchanged=True):
                if other.name != FILE_NAME:  # a conflict copy built on this computer's: the folder holds it from now
                    self._put_away(folder, other, "a sync tool's copy, taken")
                    self._save(folder)
                return self._set("saved", took=other.about())
            self._other = other
            return self._set("two_computers", other=other.about())
        self._other = None
        if FILE_NAME in unreadable:
            return self._set("waiting")
        behind = main is None or relation(main, mine) == OLDER  # not there, or a sync tool put an older one back
        changed = self._changed()
        if behind or (changed and (not wait or self._quiet())):
            self._save(folder)
        return self._set("unsaved" if self._changed() else "saved")

    def _changed(self):
        """Whether this computer's budget changed since the save it started from (it has none: it
        has). The digest is worked out again only when something was written."""
        with self.db.lock:
            mark = self.db.conn.total_changes
            if mark != self._mark:
                self._mark, self._digest = mark, backups.digest(self.db)
                self._last_change = self._clock()
            digest = self._digest
        mine = self._mine()
        return mine is None or mine.digest != digest

    def _quiet(self):
        return self._last_change is None or self._clock() - self._last_change >= self._save_after

    def _mine(self):
        """The save this computer's budget is, or started from: None before its first."""
        kept = self.db.get_meta(SAVE_KEY)
        if not kept:
            return None
        try:
            return Save.from_kept(kept)
        except (ValueError, TypeError):
            return None

    def _set_mine(self, save):
        self.db.set_meta(SAVE_KEY, replace(save, name=None, path=None).kept())

    # ── reading the folder ──

    def _read_folder(self, folder):
        """The budgets in the folder, Budget.inab first, each copied into Saves/ to be read (once:
        a file whose time and size haven't changed isn't read again). Returns (saves, the names of
        the ones that can't be read: half synced)."""
        paths = sorted((p for p in folder.iterdir() if _a_budget(p.name) and p.is_file()),
                       key=lambda p: (p.name != FILE_NAME, p.name.lower()))
        saves, unreadable = [], set()
        for path in paths:
            try:
                key = _stat_key(path)
                seen = self._seen.get(str(path))
                if seen and seen[0] == key:
                    saves.append(seen[1])
                    continue
                save = self._read(path)
                self._seen[str(path)] = (key, save)
                saves.append(save)
            except (OSError, NotABudget) as e:
                unreadable.add(path.name)
                problem = f"{path.name}: {e}"
                if problem != self._problem:
                    log.info("Couldn't read %s in the budget folder yet: %s", path.name, e)
                    self._problem = problem
        return saves, unreadable

    def _read(self, path):
        """Copies a budget from the folder into Saves/ and reads what's kept about its save."""
        saves = self._saves_dir()
        saves.mkdir(parents=True, exist_ok=True)
        reading = saves / ".reading.partial"
        _discard(reading)
        try:
            _copy(path, reading)
            save = read_save(reading)
            save.name, save.path = path.name, saves / f"{save.id}{EXTENSION}"
            os.replace(reading, save.path)
        finally:
            _discard(reading)
        return save

    def look_in(self, path):
        """What's in a folder someone chose: {'budget': who saved the budget there and when, or None,
        'moved': who moved one away from it and when, or None}. A budget that can't be read yet is
        still a budget ({'computer': None, ...})."""
        folder = Path(path)
        try:
            found = sorted((p for p in folder.iterdir() if _a_budget(p.name) and p.is_file()),
                           key=lambda p: (p.name != FILE_NAME, p.name.lower()))
        except OSError:
            found = []
        budget = None
        if found:
            try:
                budget = self._read(found[0]).about()
            except (OSError, NotABudget):
                budget = {"computer": None, "saved_at": None, "app_version": None}
        moved = _read_json(folder / MOVED_NOTE) if not found else None
        return {"budget": budget, "moved": moved and {"computer": moved.get("computer"),
                                                      "moved_at": moved.get("moved_at")}}

    # ── saving ──

    def _save(self, folder, also=()):
        """Saves this computer's budget into `folder` as Budget.inab, whole: a copy made in Saves/,
        with what's kept about the save written into it, copied into the folder under a hidden name
        and renamed over Budget.inab. `also`: other histories it was built on (the save not kept,
        changed on two computers). The copy stays in Saves/ as the save this computer started from."""
        saves = self._saves_dir()
        saves.mkdir(parents=True, exist_ok=True)
        local = saves / ".saving.partial"
        partial = folder / f".{FILE_NAME}.{_slug(self.computer)}.partial"
        _discard(local)
        try:
            with self.db.lock:  # nothing changes between the copy and its digest
                digest = backups.digest(self.db)
                self.db.conn.execute("VACUUM INTO ?", (str(local),))
            mine = self._mine()
            save_id = uuid.uuid4().hex
            save = Save(id=save_id, history=_merge([save_id], mine.history if mine else [], *also),
                        computer=self.computer, saved_at=self._now().isoformat(timespec="seconds"),
                        app_version=self.app_version, schema_version=SCHEMA_VERSION, digest=digest,
                        name=FILE_NAME, path=saves / f"{save_id}{EXTENSION}")
            _finish(local, save)
            try:
                _copy(local, partial)
                _replace(partial, folder / FILE_NAME, self._sleep)
            finally:
                _discard(partial)
            os.replace(local, save.path)
        finally:
            _discard(local)
        self._seen[str(folder / FILE_NAME)] = (_stat_key(folder / FILE_NAME), save)
        self._set_mine(save)
        self._prune_saves()
        log.info("Saved the budget to %s", folder / FILE_NAME)
        return save

    def _take(self, save, only_if_unchanged=False):
        """Makes this computer's budget `save`, another computer's, whole (db.replace_with: upgraded
        if an older version saved it, ⌘Z cleared). The upgrade isn't a change to save. False, taking
        nothing, when `only_if_unchanged` and this computer changed its budget since its last save."""
        with self.db.lock:  # nothing you do slips in between looking and taking
            if only_if_unchanged and self._changed():
                return False
            mine = self._mine()
            self.db.replace_with(save.path)
            self._mark = None
            self._set_mine(replace(save, history=_merge(save.history or [save.id], mine.history if mine else []),
                                   digest=backups.digest(self.db)))
        log.info("Took the budget saved on %s at %s (%s)", save.computer or "another computer", save.saved_at,
                 save.name)
        return True

    # ── changed on two computers ──

    def two_computers(self):
        """Changed on two computers: the three budgets each side's changes are shown from, as files
        ('started_from': the save both started from, None when this computer no longer has it;
        'this': this computer's budget as it is now; 'other': the other computer's save), and
        'other_save' (who saved it, when). None when it wasn't."""
        with self._lock:
            other = self._other
            if other is None:
                return None
            this = self._saves_dir() / THIS_COMPUTER
            _discard(this)
            with self.db.lock:
                self.db.conn.execute("VACUUM INTO ?", (str(this),))
            return {"started_from": self._started_from(other), "this": this, "other": other.path,
                    "other_save": other.about()}

    def _started_from(self, other):
        mine = self._mine()
        if mine is None:
            return None
        common = mine.id if relation(other, mine) == NEWER else next(
            (h for h in other.history if h in mine.history), None)
        path = self._saves_dir() / f"{common}{EXTENSION}" if common else None
        return path if path and path.is_file() else None

    def keep_this(self):
        """Changed on two computers: keeps this computer's budget. The other goes into Backups/, and
        this one is saved over it with both histories. Returns the state."""
        with self._lock:
            other = self._decision()
            folder = self.folder
            kept = self._back_up_file(other.path)
            if other.name != FILE_NAME:
                _remove(folder / other.name, self._sleep)
            self._save(folder, also=[other.history or [other.id]])
            self._other = None
            _discard(self._saves_dir() / THIS_COMPUTER)
            log.info("Kept this computer's budget; the one saved on %s is in %s", other.computer, kept)
            return self.check(wait=False)

    def keep_other(self):
        """Changed on two computers: keeps the other computer's budget. This computer's goes into
        Backups/ first, then the other replaces it (⌘Z cleared). Returns the state."""
        with self._lock:
            other = self._decision()
            folder = self.folder
            with self.db.lock:  # nothing you do slips in between backing up and taking
                kept = backups.save(self.db, self._not_kept_name())
                self._take(other)
            if other.name != FILE_NAME:  # a conflict copy: the folder holds it from now
                _remove(folder / other.name, self._sleep)
                self._save(folder)
            self._other = None
            _discard(self._saves_dir() / THIS_COMPUTER)
            log.info("Kept the budget saved on %s; this computer's is in %s", other.computer, kept)
            return self.check(wait=False)

    def _decision(self):
        if self._other is None or self.folder is None:
            raise UserError("There's only one budget now: nothing to choose.")
        if not _is_dir(self.folder):
            raise UserError("The budget folder can't be reached right now.")
        return self._other

    def _put_away(self, folder, save, why):
        """A conflict copy that's no longer needed goes into Backups/, not away."""
        kept = self._back_up_file(save.path)
        _remove(folder / save.name, self._sleep)
        self._seen.pop(str(folder / save.name), None)
        log.info("Moved %s (%s) to %s", save.name, why, kept)

    def _back_up_file(self, path):
        return backups.save(self.db, self._not_kept_name(), source=path)

    def _not_kept_name(self):
        name = backups.not_kept_name(self._now())
        n = 2
        while (Path(self.db.backup_dir) / name).exists():
            name = backups.not_kept_name(self._now(), n)
            n += 1
        return name

    # ── moving in, opening ──

    def move_to(self, path):
        """Puts this computer's budget into the folder at `path` (choosing one at the 1.5.0 upgrade
        or at onboarding, and Settings' Move…): saved there first, then the backups and imported
        files follow. A folder that already has a budget is never written to: UserError saying who
        saved it and when. Returns the state."""
        target = Path(path)
        with self._lock:
            if self.folder is not None and _same(target, self.folder):
                return self.check(wait=False)
            found = self.look_in(target)["budget"]
            if found:
                raise UserError(has_a_budget_message(_from_about(found)))
            old = self.folder
            if old is not None:
                kind = self.check(wait=False)["state"]
                if kind in ("two_computers", "newer", "waiting"):
                    raise UserError("Sort out the budget in its folder first: " + {
                        "two_computers": "it was changed on two computers.",
                        "newer": "a newer version saved it.",
                        "waiting": "it can't be read yet."}[kind])
            if _inside(target, self.data_dir) or _inside(target, self.db.path.parent):
                raise UserError("That folder is this computer's own data folder: choose another.")
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise UserError(f"That folder can't be used: {e.strerror or e}.") from None
            old_backups = Path(self.db.backup_dir)
            self._save(target)
            self._use(target)
            for leftovers in (old_backups, self.db.path.parent / backups.OLD_FOLDER_NAME):
                _try(f"Moving the backups from {leftovers}", lambda: backups.move_old(leftovers, target / BACKUPS))
            _try("Moving the imported files", lambda: _move_tree(self.data_dir / IMPORTED_FILES, target / IMPORTED_FILES))
            if old is not None:
                _try(f"Moving the imported files from {old}",
                     lambda: _move_tree(old / IMPORTED_FILES, target / IMPORTED_FILES))
                _try(f"Leaving {old}", lambda: self._leave(old))
            log.info("Moved the budget to %s", target)
            return self.check(wait=False)

    def _leave(self, old):
        """The folder the budget moved out of keeps a note saying so, not the budget."""
        _remove(old / self._note_name(), self._sleep)
        main = old / FILE_NAME
        if main.is_file():
            save = self._read(main)
            if relation(save, self._mine()) not in (SAME, OLDER):  # not this computer's: kept, not lost
                self._back_up_file(save.path)
            _remove(main, self._sleep)
        _write_json(old / MOVED_NOTE, {"computer": self.computer,
                                       "moved_at": self._now().isoformat(timespec="seconds")}, self._sleep)

    def open_from(self, path):
        """Opens the budget in the folder at `path` on this computer (a new computer, or one coming
        back to a folder). A budget this computer's was saved from carries on as `check` does (taken,
        or changed on two computers); any other replaces it, this computer's going into that folder's
        Backups/ first unless it's new and empty. Returns the state."""
        target = Path(path)
        with self._lock:
            main = target / FILE_NAME
            if not main.is_file():
                raise UserError("There's no budget in this folder.")
            try:
                save = self._read(main)
            except (OSError, NotABudget):
                raise UserError("The budget in this folder can't be read yet. If it's still syncing, "
                                "try again in a moment.") from None
            if save.schema_version > SCHEMA_VERSION:
                raise UserError(newer_message(save))
            mine = self._mine()  # the same budget as this computer's, or another?
            shared = mine is not None and (relation(save, mine) != APART or bool(set(save.history) & set(mine.history)))
            old = self.folder
            if old is not None and not _same(old, target):
                _try(f"Leaving {old}", lambda: _remove(old / self._note_name(), self._sleep))
            self._use(target)
            if not shared:
                with self.db.lock:
                    if not self._empty():
                        backups.save(self.db, self._not_kept_name())
                    self._take(save)
            log.info("Opened the budget in %s", target)
            return self.check(wait=False)

    def _empty(self):
        """A new budget nothing was done with yet (opening a budget replaces it without a backup)."""
        if self.db.get_meta("onboarded") == "1":
            return False
        with self.db.read() as c:
            return not any(c.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                           for table in ("accounts", "category_groups", "transactions", "imports"))

    def _use(self, folder):
        self.folder = folder
        self._other = None
        self._reachable = None
        self._noted = None
        self._point_backups()

    # ── launch and quit ──

    def at_launch(self):
        """At launch, once the database is open and before the window: backups.at_launch, then a look
        at the folder without waiting (another computer's newer save is taken before you see the
        budget; changes a session that didn't quit normally left are saved). Returns the state."""
        with self._lock:
            self._point_backups()
            backups.at_launch(self.db)
            return self.check(wait=False)

    def at_quit(self):
        """At every quit, instead of backups.at_quit: a backup if the budget changed since the newest
        one, then a save if it changed since the last save (the save then carries what's kept about
        that backup), this computer's note removed, and the write-ahead log folded in."""
        with self._lock:
            self._point_backups()
            backups.at_quit(self.db)
            try:
                self.check(wait=False)
            except Exception:
                log.exception("Saving at quit failed")
            if self._noted is not None:
                _try("Removing this computer's note", lambda: _remove(self._noted / self._note_name(), self._sleep))
                self._noted = None
            try:
                with self.db.lock:
                    self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                log.exception("Couldn't fold the write-ahead log into the database")

    # ── open on another computer ──

    def _note_name(self):
        return f".Budget open on {_slug(self.computer)}.json"

    def _note_open(self, folder):
        if self._noted is not None and _same(self._noted, folder):
            return
        self._since = self._since or self._now().isoformat(timespec="seconds")
        _write_json(folder / self._note_name(), {"computer": self.computer, "since": self._since,
                                                 "app_version": self.app_version}, self._sleep)
        self._noted = folder

    def others_open(self):
        """The other computers the budget is open on, from their notes in the folder: [{'computer',
        'since'}]. A computer that didn't quit normally leaves its note until it next quits."""
        if self.folder is None:
            return []
        try:
            paths = list(self.folder.iterdir())
        except OSError:
            return []
        found = []
        for path in paths:
            if _OPEN_NOTE.fullmatch(path.name) and path.name != self._note_name():
                note = _read_json(path)
                if note and isinstance(note.get("computer"), str):
                    found.append({"computer": note["computer"], "since": note.get("since")})
        return sorted(found, key=lambda n: n["computer"].lower())

    # ── where things go ──

    def imports_dir(self):
        """Where an imported file goes: the budget folder's Imported Files, or this computer's data
        folder's while there's no budget folder or it can't be reached (moved over when it's back)."""
        if self.folder is not None and _is_dir(self.folder):
            return self.folder / IMPORTED_FILES
        return self.data_dir / IMPORTED_FILES

    def kept_places(self, archived_path):
        """Where a file an import kept may be: where it was put, then the same month's folder in the
        budget folder's Imported Files (the path it was put at may be another computer's, even
        another system's: either kind of slash), then this data folder's."""
        parts = [p for p in re.split(r"[\\/]", archived_path) if p]
        places = [Path(archived_path)]
        if len(parts) >= 2:
            for root in (self.folder, self.data_dir):
                if root is not None:
                    places.append(root / IMPORTED_FILES / parts[-2] / parts[-1])
        return places

    def _point_backups(self):
        """Backups go to the budget folder's Backups/, or wait in the data folder's while it can't be
        reached (backups.py never makes the budget folder itself again)."""
        if self.folder is not None:
            reachable = _is_dir(self.folder)
            self.db.backup_dir = self.folder / BACKUPS if reachable else self.db.path.parent / backups.OLD_FOLDER_NAME

    def _bring_leftovers(self, folder):
        """What waited in this computer's data folder while the budget folder couldn't be reached."""
        _try("Moving the backups that waited", lambda: backups.move_old(
            self.db.path.parent / backups.OLD_FOLDER_NAME, folder / BACKUPS))
        _try("Moving the imported files that waited", lambda: _move_tree(
            self.data_dir / IMPORTED_FILES, folder / IMPORTED_FILES))

    def _saves_dir(self):
        return self.db.path.parent / SAVES

    def _prune_saves(self):
        """Saves/ keeps the save this computer started from, the other one while it's changed on two
        computers, and the KEPT_SAVES newest besides."""
        mine, other = self._mine(), self._other
        keep = {f"{s.id}{EXTENSION}" for s in (mine, other) if s}
        try:
            files = [p for p in self._saves_dir().iterdir() if p.suffix == EXTENSION and p.name != THIS_COMPUTER]
        except OSError:
            return
        spare = sorted((p for p in files if p.name not in keep), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in spare[KEPT_SAVES:]:
            _discard(path)


# ── files ──

def _a_budget(name):
    return name.lower().endswith(EXTENSION) and not name.startswith(".")


def _is_dir(path):
    try:
        return path.is_dir()
    except OSError:
        return False


def _stat_key(path):
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _same(a, b):
    try:
        return Path(a).samefile(b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _inside(path, folder):
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except OSError:
        return False


def _slug(name):
    """A computer's name as part of a file name, on any system."""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .") or "computer"


def _merge(*histories):
    seen, merged = set(), []
    for history in histories:
        for h in history:
            if h not in seen:
                seen.add(h)
                merged.append(h)
    return merged[:HISTORY_KEPT]


def _from_about(about):
    return Save(id="", computer=about.get("computer"), saved_at=about.get("saved_at"),
                app_version=about.get("app_version"))


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _finish(path, save):
    """A save's copy, as the folder gets it: what's kept about the save in, this session's undo out."""
    with closing(sqlite3.connect(str(path), isolation_level=None)) as c:
        c.execute("BEGIN")
        c.execute("DELETE FROM undo_log")
        c.execute("DELETE FROM undo_groups")
        c.execute("UPDATE undo_state SET active = 0, grp = NULL")
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (SAVE_KEY, replace(save, name=None, path=None).kept()))
        c.execute("COMMIT")


def _copy(source, target):
    """Copies a file whole, on the disk before it returns."""
    with open(source, "rb") as a, open(target, "wb") as b:
        shutil.copyfileobj(a, b, 1 << 20)
        b.flush()
        os.fsync(b.fileno())


def _replace(source, target, sleep=time.sleep, tries=REPLACE_TRIES, wait=REPLACE_WAIT):
    """os.replace, tried again while the file is held: on Windows a file another program has open
    (a sync tool uploading it, an antivirus) can't be replaced or deleted for a moment."""
    for attempt in range(tries):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            sleep(wait)


def _remove(path, sleep=time.sleep, tries=REPLACE_TRIES, wait=REPLACE_WAIT):
    for attempt in range(tries):
        try:
            Path(path).unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            sleep(wait)


def _discard(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, data, sleep=time.sleep):
    partial = path.with_name(f"{path.name}.partial")
    try:
        with open(partial, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        _replace(partial, path, sleep)
    finally:
        _discard(partial)


def _move_tree(old, new):
    """Moves every file under `old` to the same place under `new` (one already there that's the same
    isn't copied twice; another of the same name gets a free name), then removes the folders left
    empty. Returns how many moved."""
    old, new = Path(old), Path(new)
    if not old.is_dir() or (new.exists() and _same(old, new)) or _inside(new, old):
        return 0
    moved = 0
    for path in sorted(old.rglob("*")):
        if not path.is_file():
            continue
        target = new / path.relative_to(old)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if filecmp.cmp(path, target, shallow=False):
                path.unlink()
                continue
            target = _free_name(target)
        _move_file(path, target)
        moved += 1
    for folder in sorted((p for p in old.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        _try_rmdir(folder)
    _try_rmdir(old)
    if moved:
        log.info("Moved %d file%s from %s to %s", moved, "" if moved == 1 else "s", old, new)
    return moved


def _move_file(source, target):
    try:
        os.rename(source, target)  # one step on the same disk
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        partial = target.with_name(f".{target.name}.partial")
        shutil.copy2(source, partial)
        os.replace(partial, target)
        source.unlink()


def _free_name(path):
    n = 2
    while (candidate := path.with_name(f"{path.stem} ({n}){path.suffix}")).exists():
        n += 1
    return candidate


def _try_rmdir(folder):
    try:
        folder.rmdir()
    except OSError:
        pass


def _try(what, step):
    """One step of moving in or out, so the next is tried even if this one fails (the budget is
    saved by then: what's left is tidying)."""
    try:
        return step()
    except Exception as e:
        log.warning("%s failed: %s", what, e)
        return None
