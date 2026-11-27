# SPDX-License-Identifier: GPL-3.0-only
"""The budget folder (docs/adr/0008): the folder you choose for your budget, on any computer.
Whatever syncs or backs it up (OneDrive, iCloud Drive, Dropbox, a drive of your own) takes the
budget to your other computers: the app never goes online for it.

What's where:
- This computer's data folder keeps the working copy (`budget.db`, written as you work: SQLite
  in WAL mode, three files, never in the budget folder: a sync tool could catch it halfway),
  `Saves/` (the save it started from, never pruned while it is, the other computer's while it
  was changed on two computers, and the few before, `<save id>.inab`), and `computer.json`
  (this computer's id in saves: new for each budget, and when the machine isn't the one it was
  made on, `computer.machine_id`, so a copied data folder makes its own). Settings and logs too.
- The budget folder keeps `Budget.inab` (the budget, whole), `Backups/` (every computer's,
  backups.py's rules) and `Imported Files/`, and a note from each computer the budget is open on
  (`.Budget open on <computer>.json`), removed at quit: a heads-up, not a lock.

**Saving** (`_save`): a complete copy of the working copy (`VACUUM INTO`, in Saves/) with what's
kept about the save written into it (meta `save`: an id, how many saves each computer has made
in it, which computer saved it, when, the app's version and the budget format's). Then that's
recorded in the working copy as the save it started from, and only then is the copy put in the
folder, under a hidden name and renamed over Budget.inab, tried again while Windows or a sync
tool briefly holds the file (`_replace`). A crash after recording leaves the folder behind,
which the next look saves again; it never looks like another computer's save. Only when the
budget changed since the save it started from (`backups.digest`, which leaves out what's kept
about saves and backups: they aren't a change), a few seconds after changes stop
(`SAVE_AFTER`), and at quit.

**Which save came from which** (`relation`): a save is on a line, the original or a restore's,
and counts how many saves each computer has made in that line (a version vector: `counts`,
computer id → count). In one line, a save came after another when each of its counts is at least
the other's: exact however many saves there were. A restored backup (a Budget.inab no computer
saved) starts a line named after its contents (`restore:<digest>`), and its id and counts come from
its contents too, so every computer that takes it makes the same of it, whatever it had seen; it
comes after any line it isn't in. Across lines: by the lines a save says it came after
(`supersedes`), then the original before any restore's. Never by any computer's clock: two saves
neither of which came after the other are apart, a question when their contents differ.
Every budget in the folder is a save: Budget.inab, and a sync tool's conflict copy (OneDrive's
`Budget-<computer>.inab`, iCloud's `Budget 2.inab`, Dropbox's `Budget (… conflicted copy
…).inab`) the same way. Watching (`check`, every few seconds, the Downloads watcher's rhythm):
- this computer's save, or one it came after: nothing new (a conflict copy goes into Backups/);
- one that came after this computer's: taken straight away when this computer has changed
  nothing since the save it started from (the working copy becomes it, ⌘Z is cleared, as an
  import does); otherwise the budget was **changed on two computers**;
- neither (each computer saved on top of the same save, before seeing the other's): changed on
  two computers;
- an older one than this computer's in the folder, other than its own last save (a sync tool's
  "restore previous version", the same backup restored again): never saved over and never gone
  back to without asking (`older`).
A save with the very contents of this computer's budget is the same save, whatever its counts
(it's recorded as coming after both). Whenever what this computer records differs from the
folder's save (a restore taken, Keep the other one, the same contents adopted), it saves, so the
other computers see it counted; a restore taken is saved as itself, not as one more save.

**The rule** (`_replacing`, `_put_away`, `keep_this`, `_leave`, `_keep_a_copy`): a file in the
folder is saved over or removed without a copy in Backups/ only when the save written comes after
it, its contents are the very same, or a copy with its contents is in Backups/ already. A question
comes up exactly when what arrives is apart from this computer's budget (what it took, and any
change since) and its contents differ; a conflict copy this computer's save came after goes,
backed up, without one; an older Budget.inab put back with other contents is `older`.
scripts/tests/test_budget_folder_random.py checks it on random histories of three computers.
Changed on two computers, nothing is saved or taken until you choose: `two_computers()` gives the
three budgets the window shows each side's changes from (the save both started from, when this
computer still has it; this computer's working copy; the other computer's save), and
`keep_this` / `keep_other` decide, each looking at the folder again first. The one not kept goes
into Backups/ (with whatever else it would overwrite), and the save kept comes after both sides,
so each computer sees it came after its own and takes it.

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
that already has a budget is never written to. A budget that hasn't changed since its last save
goes in as that same save (Save again too), so a computer that changed it meanwhile simply comes
after it. **Opening** one (`open_from`, a new computer): this computer's budget, unless it's new
and empty, goes into that folder's Backups/ first. The same budget opened in another folder
(following a move) carries on as `check` does; the save found there was never put back, so one
older than this computer's is saved over, and what reached the old folder after the move (a copy
a question put away, a file imported while this computer's sync was behind) follows.

**Restoring a backup** (backups.py): quit the app on every computer, then copy the backup into
the budget folder as `Budget.inab`. Backups keep nothing about saves, so it's one no computer
saved: each takes it when it next looks (one with changes not saved yet asks). The same backup
restored again is the same save, older by then than the line it started: `older` asks.

**A Budget.inab that's gone** once this computer's save was in the folder (`save_landed`) is never
made again without asking (`missing`): the window offers to save it again (`save_again`) or to
choose where the budget is now.

Locks, in this order: this module's, then `Turns` (a take waits for the page's requests, which
share the budget, so it never lands between a request's read and its write), then the
database's. A request never takes this module's lock (service.FOLDER_REQUESTS don't share).
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
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

import backups
import computer
from db import SCHEMA_VERSION
from errors import UserError
from util import short_day

log = logging.getLogger("inab")

FILE_NAME = "Budget.inab"
EXTENSION = ".inab"
BACKUPS = "Backups"
IMPORTED_FILES = "Imported Files"
SAVES = "Saves"               # in the data folder
COMPUTER_FILE = "computer.json"  # in the data folder: this computer's id in saves
KEPT_SAVES = 10               # besides the one this computer started from and the other one
SAVE_AFTER = 5                # seconds without a change before saving
REPLACE_TRIES = 40            # × REPLACE_WAIT: how long a file Windows or a sync tool holds is waited for
REPLACE_WAIT = 0.25
SAVE_KEY = backups.SAVE_KEY   # meta: the save this budget is, or started from
LANDED_KEY = backups.LANDED_KEY  # meta: the save in the folder this computer's last save is or replaces
RESTORE = "restore:"          # a restored backup's key in counts: restore:<its contents' digest>
RESTORED = "restored-"        # a restored backup's id: restored-<its contents' digest>, saved back as itself
MOVED_NOTE = ".Budget moved.json"
_OPEN_NOTE = re.compile(r"\.Budget open on (.+)\.json")
_PLACEHOLDER = re.compile(r"\.(.+\.inab)\.icloud", re.IGNORECASE)  # iCloud: in the cloud, not downloaded yet

# How a save in the folder stands to this computer's (`relation`).
SAME, OLDER, NEWER, APART = "same", "older", "newer", "apart"


class NotABudget(Exception):
    """A file that isn't a whole budget: half synced, gone, or something else with the extension."""


@dataclass
class Save:
    id: str
    counts: dict = field(default_factory=dict)  # what it came after: computer id → its count, restore key → 1
    computer: str = None        # the name of the computer that saved it
    saved_at: str = None        # ISO, with the saving computer's offset from UTC
    app_version: str = None
    schema_version: int = SCHEMA_VERSION
    digest: str = None          # its contents (backups.digest), worked out here from the file
    raw: bool = False           # no computer saved it: a backup copied in to restore it
    name: str = None            # its name in the budget folder: Budget.inab, or a conflict copy's
    path: Path = None           # its copy in Saves/

    def kept(self):
        """What's kept in meta about it."""
        return json.dumps({"id": self.id, "counts": self.counts, "computer": self.computer, "saved_at": self.saved_at,
                           "app_version": self.app_version, "schema_version": self.schema_version,
                           "digest": self.digest})

    @classmethod
    def from_kept(cls, text):
        raw = json.loads(text)
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            raise ValueError("not a save")
        counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
        counts = {k: v for k, v in counts.items() if isinstance(k, str) and type(v) is int and v > 0}
        return cls(id=raw["id"], counts=counts, computer=raw.get("computer"), saved_at=raw.get("saved_at"), app_version=raw.get("app_version"),
                   schema_version=int(raw.get("schema_version") or SCHEMA_VERSION), digest=raw.get("digest"))

    def about(self):
        """Who saved it and when, for the window."""
        return {"computer": self.computer, "saved_at": self.saved_at, "app_version": self.app_version}


class Turns:
    """Requests from the page share the budget (`shared`, service.dispatch and the Downloads
    watcher); taking another computer's save, or saving this one's, has it to itself (`alone`),
    once the requests under way are done, so neither lands between a request's read and its
    write. New requests wait while one does. Both are reentrant on one thread; `alone` from inside
    a request is a bug (it would wait for itself), and raises."""

    def __init__(self):
        self._cond = threading.Condition()
        self._sharing = 0
        self._waiting = 0
        self._owner = None
        self._depth = 0
        self._here = threading.local()

    @contextmanager
    def shared(self):
        if getattr(self._here, "depth", 0) or self._owner == threading.get_ident():
            self._here.depth = getattr(self._here, "depth", 0) + 1
            try:
                yield
            finally:
                self._here.depth -= 1
            return
        with self._cond:
            while self._owner is not None or self._waiting:
                self._cond.wait()
            self._sharing += 1
        self._here.depth = 1
        try:
            yield
        finally:
            self._here.depth = 0
            with self._cond:
                self._sharing -= 1
                self._cond.notify_all()

    @contextmanager
    def alone(self):
        me = threading.get_ident()
        if self._owner == me:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        if getattr(self._here, "depth", 0):
            raise RuntimeError("Taking another computer's save from inside a request")
        with self._cond:
            self._waiting += 1
            try:
                while self._owner is not None or self._sharing:
                    self._cond.wait()
            finally:
                self._waiting -= 1
            self._owner, self._depth = me, 1
        try:
            yield
        finally:
            with self._cond:
                self._owner, self._depth = None, 0
                self._cond.notify_all()


def this_computer():
    """This computer's name as the window shows it ("Saved on …"): INAB_COMPUTER when it's set
    (the tests, two dev previews sharing one folder), else the name the system has for it."""
    name = os.environ.get("INAB_COMPUTER") or platform.node() or "This computer"
    return name[:-len(".local")] if name.endswith(".local") else name


def _covers(a, b):
    """Whether counts `a` are at least counts `b`, computer by computer."""
    return all(a.get(k, 0) >= n for k, n in b.items())


def _merge(*counts):
    merged = {}
    for c in counts:
        for k, n in c.items():
            merged[k] = max(merged.get(k, 0), n)
    return merged


def relation(save, mine):
    """How `save`, in the folder, stands to `mine`, the save this computer's budget is or started
    from: SAME, OLDER (one this computer's came after), NEWER (one that came after this computer's)
    or APART (neither: two computers each saved on top of the same one, a restore and a save it
    doesn't know of, or two different saves counting the same). Only by counts, one version vector
    over computers and restores: never by a clock."""
    if mine is None:
        return APART
    if save.id == mine.id:
        return SAME
    if save.counts == mine.counts:
        return APART
    if _covers(mine.counts, save.counts):
        return OLDER
    return NEWER if _covers(save.counts, mine.counts) else APART


def a_restore(save):
    """A backup copied in to restore it: no computer saved it, or the first to take it saved it
    back as itself (its id and counts from its contents alone)."""
    return save.raw or save.id.startswith(RESTORED)


def arriving(save, mine):
    """`relation`, for a save found in the folder: a restore as Budget.inab comes after whatever
    doesn't count it already, so every computer takes it; as a sync tool's conflict copy it's apart
    from the save it conflicts with."""
    kind = relation(save, mine)
    return NEWER if kind == APART and a_restore(save) and save.name == FILE_NAME else kind


def read_save(path):
    """What's kept about the save in the budget file at `path`, a Save (without `name` or `path`),
    and its contents' digest, worked out here. A file without it (a backup copied in to restore it)
    is a restored backup: its id and its line come from its contents, so every computer that reads
    it makes the same of it. NotABudget for a file that isn't there or isn't a whole budget."""
    try:
        with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True)) as c:  # never made
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise NotABudget("damaged")
            rows = dict(c.execute("SELECT key, value FROM meta WHERE key IN ('schema_version', ?)", (SAVE_KEY,)))
            digest = backups.digest_of(c)
        schema_version = int(rows["schema_version"])
    except (sqlite3.Error, OSError, KeyError, ValueError, TypeError) as e:
        raise NotABudget(str(e) or type(e).__name__) from None
    if rows.get(SAVE_KEY):
        try:
            save = Save.from_kept(rows[SAVE_KEY])
        except (ValueError, TypeError):
            save = None
        if save is not None:
            save.schema_version = schema_version  # the file's own: what decides whether it can be opened
            save.digest = digest
            return save
    return Save(id=f"{RESTORED}{digest[:24]}", counts={f"{RESTORE}{digest[:24]}": 1}, raw=True, digest=digest,
                schema_version=schema_version)


def _kept_about(path):
    """What's kept about a save in Saves/, read quickly (no check of the whole file), or None."""
    try:
        with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (SAVE_KEY,)).fetchone()
        return Save.from_kept(row[0]) if row and row[0] else None
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


def newer_message(save):
    """Why a budget saved by a newer version can't be opened here."""
    by = f"I Need A Budget {save.app_version}" if save.app_version else "a newer version of I Need A Budget"
    on = f" on {save.computer}" if save.computer else ""
    return f"This budget was saved by {by}{on}. Update this copy to open it."


def has_a_budget_message(about):
    if not about or not about.get("computer"):
        return "This folder already has a budget."
    return f"This folder already has a budget, saved by {about['computer']} on {when(about.get('saved_at'))}."


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
    (None until one is chosen). `computer_name` names this computer in saves and notes (a name,
    not computer.this()); `machine_id` is the machine's own (computer.machine_id by default);
    `turns` is shared with the service's requests; `on_folder(path)` is told of a new budget folder
    before anything is done in it (the service keeps it in settings.json), and told again of the old
    one when that fails; `clock`, `now` and `sleep` are for the tests."""

    def __init__(self, db, data_dir, folder=None, computer_name=None, app_version="dev", turns=None,
                 machine_id=None, on_folder=None, save_after=SAVE_AFTER, clock=time.monotonic, now=None,
                 sleep=time.sleep):
        self.db = db
        self.data_dir = Path(data_dir)
        self.computer = computer_name or this_computer()
        self._machine = machine_id
        self.app_version = app_version
        self.turns = turns or Turns()
        self._on_folder = on_folder
        self._save_after = save_after
        self._clock = clock
        self._now = now or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._lock = threading.RLock()
        self._id = None
        self._backed_up = set()    # (Backups/ folder, contents) copied there this session: never twice
        self._in_backups = {}      # a file in Backups/ → ((time, size), its contents' digest)
        self._seen = {}            # path in the folder → ((mtime, size), Save): read already
        self._other = None         # changed on two computers: the other computer's save
        self._mark = None          # db.conn.total_changes when the digest was last worked out
        self._digest = None
        self._last_change = None   # clock() when a change was last seen
        self._reachable = None     # whether the folder could be reached at the last look
        self._noted = None         # the folder this computer's note is in
        self._since = None         # when the budget was opened here, for the note
        self._problem = None       # the last problem logged, so a round that fails again isn't logged again
        self._behind = False       # the folder doesn't have this computer's last save yet (it didn't land)
        self._state = None
        self.folder = None
        if folder:
            self._use(Path(folder))

    # ── the state, for the window ──

    def state(self):
        """What the window shows about the budget folder, as the last look found it (it never looks
        itself: `check` does): {'folder', 'state', 'unsaved', 'saved', ...}. 'state' is one of: none
        (no budget folder yet), unreachable (working on this computer's copy), waiting (Budget.inab
        can't be read yet: still syncing, or not looked at yet), saved, unsaved (changes not saved
        yet), two_computers ('other': who saved the other one, when), older ('other': an older save
        put back in the folder; the window asks Use it / Keep the newer one, keep_other / keep_this),
        newer ('other': saved by a newer version, 'message'), moved ('moved': which computer moved it,
        when), missing (Budget.inab is gone from the folder: save_again, or choose where it is now).
        'saved': the save this computer's budget is or started from; 'open_elsewhere': the other
        computers it's open on."""
        state = self._state
        return dict(state) if state else self._set("none" if self.folder is None else "waiting")

    def _set(self, kind, **extra):
        mine = self._mine()
        state = {"folder": str(self.folder) if self.folder else None, "state": kind,
                 "unsaved": self.folder is not None and (self._behind or self._changed()),
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
            except (OSError, sqlite3.Error, NotABudget) as e:
                if str(e) != self._problem:
                    log.warning("Couldn't use the budget folder %s: %s", folder, e)
                    self._problem = str(e)
                return self._set("waiting")

    def _look(self, folder, wait):
        """One look (`check`). The rule it keeps: a save in the folder is put away or saved over
        without a copy in Backups/ only when this computer's comes after it or has its very contents;
        one apart from this computer's budget, with other contents, is a question, and so is an older
        Budget.inab put back."""
        self._note_open(folder)
        saves, unreadable = self._read_folder(folder)
        main = next((s for s in saves if s.name == FILE_NAME), None)
        if main is None and (FILE_NAME in unreadable or _placeholders(folder)):
            return self._set("waiting")  # still syncing, or iCloud hasn't downloaded it
        if moved := _read_json(folder / MOVED_NOTE):  # whatever else is there: the budget isn't saved here
            self._other = None
            return self._set("moved", moved={"computer": moved.get("computer"), "moved_at": moved.get("moved_at")})
        newer = next((s for s in saves if s.schema_version > SCHEMA_VERSION), None)
        if newer is not None:
            self._other = None
            return self._set("newer", other=newer.about(), message=newer_message(newer))
        mine, current = self._mine(), self._working_digest()
        others, publish = [], False
        for save in saves:  # Budget.inab first
            kind = arriving(save, mine)
            if kind in (NEWER, APART) and save.schema_version == SCHEMA_VERSION and                     save.digest in (current, mine and mine.digest):
                # The very contents of this computer's budget, or of the save it started from: nothing in it is new here
                # (another computer's Move…, its own save after a crash, the same change made on two). The same save,
                # its counts joined with this computer's, and any change this computer made since is saved on top of it.
                # Not one this computer came after: a change back to an older save's contents is still a change.
                mine, kind = self._adopt(save, mine, restore=kind == NEWER and a_restore(save)), SAME
            if kind == SAME and save.name == FILE_NAME:
                self._landed(save.id)
                publish = publish or self._differs(save)
            elif kind in (SAME, OLDER) and save.name != FILE_NAME:
                self._put_away(folder, save, "a sync tool's copy of a save this computer came after", main)
            elif not (kind == OLDER and save.name == FILE_NAME):
                others.append(save)  # newer, apart, or an older copy with contents new to this computer
        if others:
            other = others[0]
            if arriving(other, mine) == NEWER and self._take(other, only_if_unchanged=True):
                if other.name != FILE_NAME:  # a conflict copy built on this computer's: the folder holds it from now
                    self._put_away(folder, other, "a sync tool's copy, taken")
                    self._save(folder)
                elif self._differs(other):
                    self._save(folder, as_is=self._as_is(other))  # what this computer records, in the folder too
                return self._set("saved", took=other.about())
            self._other = other
            self._prune_saves()
            return self._set("two_computers", other=other.about())
        if FILE_NAME in unreadable:
            self._other = None
            return self._set("waiting")
        landed = self.db.get_meta(LANDED_KEY)  # after the loop: Budget.inab adopted just now has landed
        if main is not None and relation(main, mine) == OLDER and main.id != landed and                 main.digest not in (current, mine.digest):
            self._other = main  # an older one put back: never saved over, nor gone back to, without asking
            self._prune_saves()
            return self._set("older", other=main.about())
        self._other = None
        if main is None and landed:
            return self._set("missing")  # once this computer's save was there: never made again without asking
        behind = main is None or relation(main, mine) == OLDER  # not there yet, or this computer's save didn't land
        self._behind = behind and mine is not None
        if behind or publish or (self._changed() and (not wait or self._quiet())):
            self._save(folder, as_is=publish and main is not None and self._as_is(main) and not self._changed())
        return self._set("unsaved" if self._behind or self._changed() else "saved")

    def _differs(self, save):
        """Whether the folder's `save` doesn't say what this computer records about it (a restored
        backup taken, Keep the other one, the same contents adopted): then it's saved again."""
        mine = self._mine()
        return save.raw or mine is None or mine.counts != save.counts

    def _as_is(self, save):
        """Whether this computer's budget is the restored backup `save` exactly as every computer takes
        it (its counts the restore's alone): then it's saved as itself, not as a new save."""
        mine = self._mine()
        return save.raw and mine is not None and (mine.id, mine.counts) == (save.id, save.counts)

    def _adopt(self, save, mine, restore=False):
        """A save with the very contents of this computer's budget (its own save after a crash, two
        computers making one change) is the same save: recorded as coming after both, no take. A
        restore arriving is taken as the restore: its counts alone, as every computer takes it."""
        counts = dict(save.counts) if restore else _merge(save.counts, mine.counts if mine else {})
        adopted = replace(save, counts=counts, raw=False, name=None, path=None)
        self._set_mine(adopted)
        if save.name == FILE_NAME:
            self._landed(save.id)
        log.info("The budget saved on %s at %s is the same as this computer's", save.computer, save.saved_at)
        return adopted

    def _changed(self):
        """Whether this computer's budget changed since the save it started from (it has none: it
        has). The digest is worked out again only when something was written. The first time (at
        launch, after taking a save) isn't a change seen: what an earlier session left is saved at once."""
        with self.db.lock:
            mark = self.db.conn.total_changes
            if mark != self._mark:
                if self._mark is not None:
                    self._last_change = self._clock()
                self._mark, self._digest = mark, backups.digest(self.db)
            digest = self._digest
        mine = self._mine()
        return mine is None or mine.digest != digest

    def _unchanged(self):
        """Nothing changed since the save this computer started from: saving it again (Move…, Save
        again) is that same save, never a change another computer's would be apart from."""
        return self._mine() is not None and not self._changed()

    def _working_digest(self):
        self._changed()
        return self._digest

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
        self.db.set_meta(SAVE_KEY, replace(save, raw=False, name=None, path=None).kept())

    def _landed(self, save_id):
        if self.db.get_meta(LANDED_KEY) != save_id:
            self.db.set_meta(LANDED_KEY, save_id)

    def machine_id(self):
        if self._machine is None:
            try:
                self._machine = computer.this().machine_id() or ""
            except Exception:  # a system that won't say: the id isn't tied to the machine
                self._machine = ""
        return self._machine or None

    def computer_id(self):
        """This computer's id in saves' counts, kept in the data folder (outside the budget, which
        another computer's save replaces) with the machine's own id: a data folder copied to another
        machine makes a new one there, so two computers never count as one."""
        if self._id is None:
            kept = _read_json(self.db.path.parent / COMPUTER_FILE) or {}
            if isinstance(kept.get("id"), str) and kept["id"] and kept.get("machine") == self.machine_id():
                self._id = kept["id"]
            else:
                self._new_id()
        return self._id

    def _new_id(self):
        """A new id for this computer, for a budget it starts (no save yet) or opens from scratch: a
        budget started again never counts as the old one's saves."""
        self._id = uuid.uuid4().hex
        _write_json(self.db.path.parent / COMPUTER_FILE, {"id": self._id, "machine": self.machine_id()}, self._sleep)

    def _next_count(self, counts):
        """This computer's count for its next save: after any it ever gave one in this budget (kept
        with its id), so after a restore taken, whose counts don't have it, it never counts from one
        again: two of its saves never count the same."""
        me = self.computer_id()
        kept = _read_json(self.db.path.parent / COMPUTER_FILE) or {}
        last = kept["count"] if kept.get("id") == me and type(kept.get("count")) is int else 0
        count = max(counts.get(me, 0), last) + 1
        _write_json(self.db.path.parent / COMPUTER_FILE, {"id": me, "machine": self.machine_id(), "count": count},
                    self._sleep)
        return count

    # ── reading the folder ──

    def _read_folder(self, folder):
        """The budgets in the folder, Budget.inab first, each copied into Saves/ to be read (once:
        a file whose time and size haven't changed, and whose copy is still there, isn't read again).
        Returns (saves, the names of the ones that can't be read: half synced)."""
        saves, unreadable, read = [], set(), False
        for path in _budget_files(folder):
            try:
                seen = self._seen.get(str(path))
                if seen and seen[0] == _stat_key(path) and seen[1].path.is_file():
                    saves.append(seen[1])
                    continue
                save, key = self._read(path)
                self._seen[str(path)] = (key, save)
                saves.append(save)
                read = True
            except (OSError, NotABudget) as e:
                self._seen.pop(str(path), None)
                unreadable.add(path.name)
                problem = f"{path.name}: {e}"
                if problem != self._problem:
                    log.info("Couldn't read %s in the budget folder yet: %s", path.name, e)
                    self._problem = problem
        if read:
            self._prune_saves(keep=saves)
        return saves, unreadable

    def _read(self, path):
        """Copies a budget from the folder into Saves/ and reads what's kept about its save.
        Returns (the Save, the file's time and size). NotABudget when it changed while being
        copied (a sync tool writing it): it's read again at the next look."""
        saves = self._saves_dir()
        saves.mkdir(parents=True, exist_ok=True)
        reading = saves / f".reading-{uuid.uuid4().hex}.partial"
        try:
            key = _stat_key(path)
            _copy(path, reading)
            if _stat_key(path) != key:
                raise NotABudget("it changed while it was read")
            save = read_save(reading)
            save.name, save.path = path.name, saves / f"{save.id}{EXTENSION}"
            _replace(reading, save.path, self._sleep)
        finally:
            _discard(reading)
        return save, key

    def look_in(self, path):
        """What's in a folder someone chose: {'budget': who saved the budget there and when, or None,
        'moved': who moved one away from it and when, or None}. A budget that can't be read yet (still
        syncing, or in iCloud and not downloaded) is still a budget ({'computer': None, ...})."""
        folder = Path(path)
        with self._lock:
            try:
                found = _budget_files(folder)
                waiting = _placeholders(folder)
            except OSError:
                found, waiting = [], []
            budget = None
            if found:
                try:
                    budget = self._read(found[0])[0].about()
                except (OSError, NotABudget):
                    budget = {"computer": None, "saved_at": None, "app_version": None}
            elif waiting:
                budget = {"computer": None, "saved_at": None, "app_version": None}
            moved = _read_json(folder / MOVED_NOTE) if budget is None else None
            return {"budget": budget, "moved": moved and {"computer": moved.get("computer"),
                                                          "moved_at": moved.get("moved_at")}}

    # ── saving ──

    def _save(self, folder, over=(), as_is=False):
        """Saves this computer's budget into `folder` as Budget.inab, whole: a copy made in Saves/
        with what's kept about the save written into it, recorded as the save this computer started
        from, then copied into the folder under a hidden name and renamed over Budget.inab. `over`:
        the saves it overrides (the one not kept, changed on two computers), which it comes after.
        `as_is`: the save this computer started from, saved as itself (its own id and counts): a
        restored backup just taken (every computer that takes it makes the same), or a budget that
        hasn't changed since, so it isn't one more save to take."""
        saves = self._saves_dir()
        saves.mkdir(parents=True, exist_ok=True)
        local = saves / f".saving-{uuid.uuid4().hex}.partial"
        partial = folder / f".{FILE_NAME}.{_slug(self.computer)}.partial"
        try:
            with self.turns.alone(), self.db.lock:  # nothing changes between the copy and its digest, nor mid-request
                digest = backups.digest(self.db)
                self.db.conn.execute("VACUUM INTO ?", (str(local),))
            mine = self._mine()
            if as_is and (mine is None or mine.digest != digest):
                as_is = False  # it changed since (a request slipped in): never other contents under the same save
            stamp = self._now().isoformat(timespec="seconds")
            if as_is:
                save = replace(mine, computer=self.computer, saved_at=stamp, app_version=self.app_version,
                               schema_version=SCHEMA_VERSION, digest=digest, raw=False, name=FILE_NAME,
                               path=saves / f"{mine.id}{EXTENSION}")
            else:
                if mine is None:
                    self._new_id()  # a budget's first save: this computer's count in it starts again
                counts = _merge(mine.counts if mine else {}, *(o.counts for o in over))
                counts[self.computer_id()] = self._next_count(counts)
                save_id = uuid.uuid4().hex
                save = Save(id=save_id, counts=counts,
                            computer=self.computer, saved_at=stamp, app_version=self.app_version,
                            schema_version=SCHEMA_VERSION, digest=digest, name=FILE_NAME,
                            path=saves / f"{save_id}{EXTENSION}")
            _finish(local, save)
            _replace(local, save.path, self._sleep)
        finally:
            _discard(local)
        self._replacing(folder, save)  # first: a crash after this tries again, never asks about what it chose
        self._set_mine(save)     # then: a crash after this leaves the folder behind, saved again next look
        self._behind = True
        try:
            _copy(save.path, partial)
            _replace(partial, folder / FILE_NAME, self._sleep)
        finally:
            _discard(partial)
        self._landed(save.id)
        self._behind = False
        self._seen[str(folder / FILE_NAME)] = (_stat_key(folder / FILE_NAME), save)
        self._prune_saves()
        log.info("Saved the budget to %s", folder / FILE_NAME)
        return save

    def _replacing(self, folder, save):
        """Before `save` goes into the folder: the save there now is noted as one this computer chose
        to save over, so if writing fails it's simply tried again (a choice made stays made). It goes
        into Backups/ first unless `save` comes after it or has its very contents (`_keep_a_copy`)."""
        target = folder / FILE_NAME
        try:
            key = _stat_key(target)
        except OSError:
            return
        seen = self._seen.get(str(target))
        here = seen[1] if seen and seen[0] == key and seen[1].path and seen[1].path.is_file() else None
        if here is None:
            try:
                here, key = self._read(target)
                self._seen[str(target)] = (key, here)
            except (OSError, NotABudget):
                return
        if relation(here, save) not in (SAME, OLDER) and here.digest != save.digest:
            self._keep_a_copy(here)
        self._landed(here.id)

    def _keep_a_copy(self, save):
        """Copies a save from the folder into Backups/ before it's saved over or removed, unless a
        copy with its very contents is there already (whichever computer made it)."""
        here = os.path.normcase(os.path.abspath(self.db.backup_dir))  # a copy counts only in the Backups/ in use now
        if (here, save.digest) in self._backed_up or save.digest in self._backup_digests():
            return
        self._back_up_file(save.path)
        self._backed_up.add((here, save.digest))

    def _keep_this_one(self, unless):
        """This computer's budget into Backups/ before something replaces it, unless it has the very
        contents of what replaces it (`unless`) or a copy of them is there already. The path, or None."""
        digest = self._working_digest()
        here = os.path.normcase(os.path.abspath(self.db.backup_dir))
        if digest == unless or (here, digest) in self._backed_up or digest in self._backup_digests():
            return None
        kept = backups.save(self.db, self._not_kept_name())
        self._backed_up.add((here, digest))
        return kept

    def _backup_digests(self):
        """The contents of the budgets in Backups/, each worked out once (by its time and size)."""
        found = set()
        try:
            paths = [p for p in Path(self.db.backup_dir).iterdir() if p.suffix == ".db" and p.is_file()]
        except OSError:
            return found
        for path in paths:
            try:
                stat = path.stat()
                key = (stat.st_mtime_ns, stat.st_size)
                kept = self._in_backups.get(str(path))
                if kept is None or kept[0] != key:
                    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as c:
                        kept = (key, backups.digest_of(c))
                    self._in_backups[str(path)] = kept
                found.add(kept[1])
            except (OSError, sqlite3.Error):
                continue
        return found

    def _take(self, save, only_if_unchanged=False, fresh=False, chosen=False):
        """Makes this computer's budget `save`, another computer's (or a restored backup), whole
        (db.replace_with: upgraded if an older version saved it, ⌘Z cleared), once the page's requests
        under way are done. Its copy in Saves/ is checked to be that save first. The upgrade isn't a
        change to save. A newer save, or one in this computer's line, carries on in its line; an older
        or apart one taken by choice (Keep the other one, Use it) is recorded as coming after this
        computer's, so its next save is taken by the others. False, taking nothing, when
        `only_if_unchanged` and this computer changed its budget since its last save. NotABudget when
        its copy isn't there or isn't it (it's read again next look). `fresh`: another budget
        (open_from), taken as it is."""
        with self.turns.alone(), self.db.lock:  # nothing you do slips in between looking and taking
            if only_if_unchanged and self._changed():
                return False
            try:
                found = read_save(save.path)
                if found.id != save.id:
                    raise NotABudget(f"its copy in {SAVES}/ is another budget")
            except NotABudget:
                self._forget(save)
                raise
            mine = None if fresh else self._mine()
            landed = self.db.get_meta(LANDED_KEY)  # this computer's, not the budget's: kept across the take
            restore = a_restore(save) and not chosen  # a backup copied in, taken as every computer takes it
            if a_restore(save) and not fresh and not (mine and _covers(save.counts, mine.counts)):
                self._keep_this_one(found.digest)  # a restore taken: this computer's budget is kept first
            self.db.replace_with(save.path)
            self._mark = None
            # Another budget, or a restore: its counts alone are this computer's from now (nothing it had is
            # joined in, so a save that comes after the restore is taken here). Otherwise after both: a newer
            # save's own counts, or one taken by choice (Keep the other one, Use it) counted after this one's.
            counts = dict(save.counts) if fresh or restore or mine is None else _merge(save.counts, mine.counts)
            self._set_mine(replace(save, counts=counts, raw=False, digest=backups.digest(self.db)))
            if save.name == FILE_NAME:
                self._landed(save.id)
            elif landed and not fresh:
                self._landed(landed)
        log.info("Took the budget saved on %s at %s (%s)", save.computer or "no computer: restored",
                 save.saved_at, save.name)
        return True

    def _forget(self, save):
        self._seen = {k: v for k, v in self._seen.items() if v[1].id != save.id}
        if save.path is not None:
            _discard(save.path)

    # ── changed on two computers ──

    def two_computers(self):
        """Changed on two computers: the three budgets each side's changes are shown from
        (changes.compare): 'started_from', the copy of the save both started from (None when this
        computer no longer has it: then 'can_show_changes' is False, and the window asks which to
        keep without showing what each changed); 'this', this computer's working copy (its Database);
        'other', the other computer's save; and 'other_save' (who saved it, when). None when it wasn't."""
        with self._lock:
            other = self._other
            if other is None:
                return None
            start = self._started_from(other)
            return {"started_from": start, "this": self.db, "other": other.path, "other_save": other.about(),
                    "can_show_changes": start is not None}

    def _started_from(self, other):
        mine = self._mine()
        if mine is None:
            return None
        kind = arriving(other, mine)
        if kind == OLDER:  # an older one put back: what this computer changed since
            return other.path if other.path and other.path.is_file() else None
        if kind == NEWER:
            path = self._saves_dir() / f"{mine.id}{EXTENSION}"
            return path if path.is_file() else None
        best = None  # the newest save kept here that both came after
        for path in self._saves_dir().glob(f"*{EXTENSION}"):
            save = _kept_about(path)
            if save and save.id != other.id and relation(save, mine) in (SAME, OLDER) and \
                    relation(save, other) in (SAME, OLDER):
                if best is None or relation(save, best[0]) == NEWER:
                    best = (save, path)
        return best[1] if best else None

    def keep_this(self):
        """Changed on two computers (or an older one put back: Keep the newer one): keeps this
        computer's budget. The folder is looked at again first: the other one, and whatever else
        this computer's budget didn't come after, goes into Backups/, and this one is saved over
        them, coming after them all. Returns the state."""
        with self._lock:
            other = self._decision()
            folder = self.folder
            if not (other.path and other.path.is_file()):
                self._forget(other)
                self._other = None
                raise UserError("The other computer's budget can't be read just now: try again in a moment.")
            saves, unreadable = self._read_folder(folder)
            if FILE_NAME in unreadable or _placeholders(folder):
                raise UserError("The budget in its folder can't be read just now: try again in a moment.")
            newer = next((s for s in saves if s.schema_version > SCHEMA_VERSION), None)
            if newer is not None:
                self._other = None
                raise UserError(newer_message(newer))
            mine = self._mine()
            over = [other] + [s for s in saves if s.id != other.id and relation(s, mine) not in (SAME, OLDER)]
            for save in over:
                self._keep_a_copy(save)
            for save in over:
                if save.name != FILE_NAME:
                    _remove(folder / save.name, self._sleep)
            self._other = None
            self._saved_choice(folder, over=over)
            log.info("Kept this computer's budget; the one saved on %s is in Backups", other.computer)
            return self.check(wait=False)

    def keep_other(self):
        """Changed on two computers (or an older one put back: Use it): keeps the other budget. The
        folder is looked at again first: if the other computer saved again since the question, its
        newest is the one kept. This computer's goes into Backups/, then the other replaces it (⌘Z
        cleared), and it's saved again when what this computer records differs from the folder's (so
        the other computers see this one counted, and take it). Returns the state."""
        with self._lock:
            other = self._decision()
            folder = self.folder
            saves, unreadable = self._read_folder(folder)
            if FILE_NAME in unreadable or _placeholders(folder):
                raise UserError("The budget in its folder can't be read just now: try again in a moment.")
            newer = next((s for s in saves if s.schema_version > SCHEMA_VERSION), None)
            if newer is not None:
                self._other = None
                raise UserError(newer_message(newer))
            target = next((s for s in saves if s.id != other.id and relation(s, other) == NEWER), other)
            try:
                with self.turns.alone(), self.db.lock:  # nothing you do slips in between backing up and taking
                    read_save(target.path)
                    kept = self._keep_this_one(target.digest)
                    self._take(target, chosen=True)
            except NotABudget:
                self._forget(target)
                self._other = None
                raise UserError("The other computer's budget can't be read just now: try again in a moment.") from None
            self._other = None
            if target.name != FILE_NAME:  # a conflict copy: the folder holds it from now
                _remove(folder / target.name, self._sleep)
                self._saved_choice(folder)
            elif self._differs(target):
                self._saved_choice(folder, as_is=self._as_is(target))
            log.info("Kept the budget saved on %s; this computer's is in %s", target.computer, kept or "Backups")
            return self.check(wait=False)

    def _saved_choice(self, folder, over=(), as_is=False):
        """The save a choice makes. It's recorded before the folder is written (`_save`), so when the
        file is held too long the choice stands and the save is tried again at the next look."""
        try:
            self._save(folder, over, as_is)
        except OSError as e:
            log.warning("The budget kept couldn't be saved in %s yet: %s", folder, e)

    def _decision(self):
        if self._other is None or self.folder is None:
            raise UserError("There's only one budget now: nothing to choose.")
        if not _is_dir(self.folder):
            raise UserError("The budget folder can't be reached right now.")
        if _read_json(self.folder / MOVED_NOTE):  # moved since the question: it's asked again where it is now
            self._other = None
            self.check(wait=False)
            raise UserError("The budget was moved to another folder since: open it from there.")
        return self._other

    def _put_away(self, folder, save, why, main=None):
        """A conflict copy that's no longer needed goes, into Backups/ unless Budget.inab (`main`) has
        its very contents or a copy of them is there already."""
        if main is None or main.digest != save.digest:
            self._keep_a_copy(save)
        _remove(folder / save.name, self._sleep)
        self._seen.pop(str(folder / save.name), None)
        log.info("Moved %s (%s) to Backups", save.name, why)

    def _back_up_file(self, path):
        return backups.save(self.db, self._not_kept_name(), source=path)

    def _not_kept_name(self):
        tag = getattr(self.db, "backup_tag", None)
        name, n = backups.not_kept_name(self._now(), tag=tag), 2
        while (Path(self.db.backup_dir) / name).exists():
            name, n = backups.not_kept_name(self._now(), n, tag=tag), n + 1
        return name

    # ── moving in, opening ──

    def move_to(self, path, old_folder_gone=False):
        """Puts this computer's budget into the folder at `path` (choosing one at the 1.5.0 upgrade
        or at onboarding, and Settings' Move…): saved there first, then the backups and imported
        files follow. A folder that already has a budget is never written to: UserError saying who
        saved it and when. A budget folder that can't be reached is left only when the person says
        it's gone for good (`old_folder_gone`): a computer still using it wouldn't learn of the move.
        Returns the state."""
        target = Path(path)
        with self._lock:
            if self.folder is not None and _same(target, self.folder):
                return self.check(wait=False)
            found = self.look_in(target)["budget"]
            if found:
                raise UserError(has_a_budget_message(found))
            old = self.folder
            if old is not None:
                kind = self.check(wait=False)["state"]
                if kind == "unreachable" and old_folder_gone:
                    old = None
                elif kind in ("two_computers", "older", "newer", "waiting", "unreachable"):
                    raise UserError({
                        "two_computers": "First choose which budget to keep: it was changed on two computers.",
                        "older": "First choose which budget to keep: an older one was put back in its folder.",
                        "newer": "First update this copy: a newer version saved the budget.",
                        "waiting": "The budget in its folder can't be read just now: try again in a moment.",
                        "unreachable": f"The budget folder ({self.folder}) can't be reached. Connect it first, "
                                       "or move the budget anyway if that folder is gone for good."}[kind])
            if _inside(target, self.data_dir) or _inside(target, self.db.path.parent):
                raise UserError("That folder is this computer's own data folder: choose another.")
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise UserError(f"That folder can't be used: {e.strerror or e}.") from None
            old_backups = Path(self.db.backup_dir)
            self._announce(target)  # first: a crash from here on relaunches on the new folder
            try:
                self._save(target, as_is=self._unchanged())
            except BaseException:
                self._announce(self.folder)
                raise
            _remove(target / MOVED_NOTE, self._sleep)  # one an earlier move out of it left
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
        """The folder the budget moved out of keeps a note saying so, not the budget. The note
        first: a computer that sees Budget.inab go before the note arrives doesn't put it back
        (`missing`), and once the note is there, it never saves there. A Budget.inab this computer's
        save didn't come after goes into Backups/ first."""
        _write_json(old / MOVED_NOTE, {"computer": self.computer,
                                       "moved_at": self._now().isoformat(timespec="seconds")}, self._sleep)
        main = old / FILE_NAME
        if main.is_file():
            save, mine = self._read(main)[0], self._mine()
            if relation(save, mine) not in (SAME, OLDER) and save.digest != (mine and mine.digest):
                self._keep_a_copy(save)
            _remove(main, self._sleep)
        _remove(old / self._note_name(), self._sleep)

    def open_from(self, path):
        """Opens the budget in the folder at `path` on this computer (a new computer, or one coming
        back to a folder). The same budget as this computer's carries on as `check` does (taken, or
        changed on two computers); any other replaces it, this computer's going into that folder's
        Backups/ first unless it's new and empty. Returns the state."""
        target = Path(path)
        with self._lock:
            main = target / FILE_NAME
            if not main.is_file():
                if _placeholders(target):
                    raise UserError("The budget in this folder isn't downloaded from iCloud yet. "
                                    "Try again in a moment.")
                raise UserError("There's no budget in this folder.")
            try:
                save = self._read(main)[0]
            except (OSError, NotABudget):
                raise UserError("The budget in this folder can't be read yet. If it's still syncing, "
                                "try again in a moment.") from None
            if save.schema_version > SCHEMA_VERSION:
                raise UserError(newer_message(save))
            # The same budget as this computer's only when both count something only it has: a computer's id
            # (new for each budget), a restore's key (from its contents). Never by which looks older.
            mine, old = self._mine(), self.folder
            following = old is not None and not _same(old, target) and bool(_read_json(old / MOVED_NOTE))
            shared = mine is not None and (
                (old is not None and _same(old, target))  # its own folder: the same budget, whatever's in it
                or following  # where it moved (a restore there since may count nothing this computer knows): asked, never replaced
                or bool(set(save.counts) & set(mine.counts)))
            self._announce(target)  # first: a crash from here on relaunches on this folder, not the old one
            try:
                if old is not None and not _same(old, target):
                    _try(f"Leaving {old}", lambda: _remove(old / self._note_name(), self._sleep))
                self._use(target)
                if shared and not (old is not None and _same(old, target)):
                    self._landed(save.id)  # what's there on arriving was never put back: older is saved over
                    if following:
                        self._bring_from(old, target)  # what reached the old folder since the move comes too
                if not shared:
                    with self.turns.alone(), self.db.lock:
                        if not self._empty():
                            backups.save(self.db, self._not_kept_name())
                        self._take(save, fresh=True)
                    self._new_id()  # another budget: this computer's saves in it start a line of their own
            except BaseException:
                if old is not None:
                    self._use(old)
                self._announce(old)
                raise
            log.info("Opened the budget in %s", target)
            return self.check(wait=False)

    def _announce(self, folder):
        if self._on_folder is not None:
            self._on_folder(folder)

    def save_again(self):
        """Budget.inab is gone from the folder (`missing`): saves this computer's budget there again."""
        with self._lock:
            if self.folder is None or not _is_dir(self.folder):
                raise UserError("The budget folder can't be reached right now.")
            if not (self.folder / FILE_NAME).exists() and not _read_json(self.folder / MOVED_NOTE):
                self._save(self.folder, as_is=self._unchanged())
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
        self._behind = False
        self._reachable = None
        self._noted = None
        self.db.backup_tag = computer_tag(self.computer)  # two computers' backups in one folder never share a name
        self.db.backups_in_budget_folder = True  # a backup never makes the budget folder again (backups._copy_into)
        self._point_backups()

    # ── launch and quit ──

    def at_launch(self):
        """At launch, once the database is open and before the window: backups.at_launch, then a look
        at the folder without waiting (another computer's newer save is taken before you see the
        budget; changes a session that didn't quit normally left are saved). Returns the state."""
        with self._lock:
            self._point_backups()
            backups.at_launch(self.db, self._clock_time())
            return self.check(wait=False)

    def at_quit(self):
        """At every quit, instead of backups.at_quit: a backup if the budget changed since the newest
        one, then a save if it changed since the last save, this computer's note removed, and the
        write-ahead log folded in."""
        with self._lock:
            self._point_backups()
            backups.at_quit(self.db, self._clock_time())
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

    def _clock_time(self):
        """This computer's time, for backups' names and their age alike: the not-kept copies a question
        made are named by it, and must never look old to a second clock."""
        return self._now().replace(tzinfo=None)

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

    def _bring_from(self, old, folder):
        """What went into the folder the budget moved out of after the move, from a computer that
        hadn't heard of it yet (a copy a question put away, a file it imported), follows the budget."""
        _try(f"Moving the backups left in {old}", lambda: backups.move_old(old / BACKUPS, folder / BACKUPS))
        _try(f"Moving the imported files left in {old}", lambda: _move_tree(
            old / IMPORTED_FILES, folder / IMPORTED_FILES))

    def _saves_dir(self):
        return self.db.path.parent / SAVES

    def _prune_saves(self, keep=()):
        """Saves/ keeps the save this computer started from (never pruned while it is), the other one
        while it was changed on two computers, `keep` (what's in the folder now), and the KEPT_SAVES
        newest besides: two saves made at once started from one before either. Copies left half made
        go: every read and save here is made under this module's lock."""
        kept = {f"{s.id}{EXTENSION}" for s in (self._mine(), self._other, *keep) if s}
        try:
            files = list(self._saves_dir().iterdir())
        except OSError:
            return
        spare = []
        for path in files:
            if ".partial" in path.name:  # a copy left half made, or the journal SQLite left beside one
                _discard(path)
            elif path.suffix == EXTENSION and path.name not in kept:
                spare.append(path)
        spare.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for path in spare[KEPT_SAVES:]:
            _discard(path)


# ── files ──

def _budget_files(folder):
    """The budgets in a folder, Budget.inab first (iCloud's placeholders and hidden files aren't)."""
    found = [p for p in Path(folder).iterdir()
             if p.name.lower().endswith(EXTENSION) and not p.name.startswith(".") and p.is_file()]
    return sorted(found, key=lambda p: (p.name != FILE_NAME, p.name.lower()))


def _placeholders(folder):
    """iCloud's stand-ins for budgets it hasn't downloaded yet (`.Budget.inab.icloud`)."""
    try:
        return [p for p in Path(folder).iterdir() if _PLACEHOLDER.fullmatch(p.name)]
    except OSError:
        return []


def _is_dir(path):
    try:
        return path.is_dir()
    except OSError:
        return False


def _stat_key(path):
    """What tells a budget file in the folder from the one read before: its time and size, and its first
    64 KB (where its `save` meta is), since two saves can share a size and be written within one tick of
    the clock (a sync tool's "restore previous version" over a save just made)."""
    stat = path.stat()
    with open(path, "rb") as f:
        head = hashlib.sha256(f.read(1 << 16)).hexdigest()
    return stat.st_mtime_ns, stat.st_size, head


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


def computer_tag(name):
    """What a computer's backups' names end with (db.Database's `backup_tag`), so main.py can give
    the database it before the budget folder sets it: the copy made before an upgrade is named too."""
    return _slug(name)


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
        c.execute("DELETE FROM meta WHERE key = ?", (LANDED_KEY,))  # this computer's own
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
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{uuid.uuid4().hex}.partial")
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
