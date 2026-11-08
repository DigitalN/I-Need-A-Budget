# SPDX-License-Identifier: GPL-3.0-only
"""Backups: a complete copy of the budget at every quit, kept four months (docs/adr/0004).

They go to `~/Documents/I Need A Budget Backups`, which iCloud Drive can back up off the
Mac. The live budget never moves there: it's written as you work (SQLite in WAL mode, so
three files), and a sync could catch it halfway. A backup is one complete file, made with
`VACUUM INTO` under a hidden name and renamed when it's done, so the folder never holds
half a copy under a backup's name.

- `at_quit`: every quit (main.py: the close button, ⌘Q, Restart now, quitting to install
  an update).
- `at_launch`: only when the budget changed since the newest backup (`changed`), which
  means the last session didn't quit normally (a crash, a power cut). It also moves the
  backups from the data folder's old `Backups/` (where they were until 1.3.1) over, once.
- The copy made before a database upgrade (db.py) goes to the same folder, under its own
  name, and is kept as long as the others.
- `prune` keeps every backup from the last four months. The newest from before today is
  never deleted for its age, so a long break can't empty the folder.

Backups are named with the date and time they were made: `budget-2026-11-08-14-32-05.db`,
`budget-before-upgrade-v7-to-v8-2026-11-08-14-32-05.db` (and the old daily
`budget-2026-10-12.db`, or `budget-2026-10-12 (2).db` after a name clash moving them over).
Only files named like that are ever deleted, with the side files SQLite leaves beside a
backup someone opened in place (`-wal`, `-shm`, `-journal`): they go with their backup, or
as soon as it's gone. Copies left half made go too: ours at once, the old daily backup's
(`budget-2026-10-12.tmp`) after four months.

If the folder can't be written (Documents access refused in System Settings), backups go
to the old `Backups/` in the data folder instead, and the next launch moves them over. That
is one line in the log each time, not a traceback.

With INAB_DATA_DIR set, main.py puts the folder inside that folder, so tests, the dev
preview and test copies never touch the real Documents. `Database` defaults to there too.

To restore one: quit the app, delete `budget.db-wal` and `budget.db-shm` in the data
folder if they're there, and copy the backup in as `budget.db`.
"""

import calendar
import errno
import filecmp
import hashlib
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger("inab")

FOLDER_NAME = "I Need A Budget Backups"
OLD_FOLDER_NAME = "Backups"   # in the data folder, until 1.3.1
KEPT_MONTHS = 4
DIGEST_KEY = "backed_up"      # meta: `digest` of the budget when the last backup was made
NOT_THE_BUDGET = ("undo_log", "undo_groups", "undo_state")  # this session's undo, cleared at launch
SIDE_FILES = ("-wal", "-shm", "-journal")  # what SQLite leaves beside a database someone opened

_STAMP = "%Y-%m-%d-%H-%M-%S"
_MADE_AT = re.compile(
    r"budget-(?:before-upgrade-v\d+-to-v\d+-)?(\d{4}-\d{2}-\d{2})((?:-\d{2}){3})?(?: \(\d+\))?\.db")
_HALF_MADE = re.compile(r"budget-(\d{4}-\d{2}-\d{2})\.tmp")  # by the daily backup, until 1.3.1


def name(now):
    return f"budget-{now.strftime(_STAMP)}.db"


def upgrade_name(old_version, new_version, now):
    return f"budget-before-upgrade-v{old_version}-to-v{new_version}-{now.strftime(_STAMP)}.db"


def made_at(filename):
    """When a backup called `filename` was made, or None for a file that isn't a backup."""
    match = _MADE_AT.fullmatch(filename)
    if not match:
        return None
    try:
        return datetime.strptime(match[1] + (match[2] or "-00-00-00"), _STAMP)
    except ValueError:
        return None


def _main_of(path):
    """The database a side file belongs to, or None for a file that isn't a side file."""
    for suffix in SIDE_FILES:
        if path.name.endswith(suffix):
            return path.with_name(path.name[:-len(suffix)])
    return None


def _partial(path):
    """A copy left half made (`_copy_into`, `_move`), or a side file of one."""
    name = (_main_of(path) or path).name
    return name.startswith(".") and name.endswith(".partial")


def found(folder):
    """The backups in `folder`, oldest first, as (when it was made, path)."""
    try:
        paths = list(Path(folder).iterdir())
    except OSError:
        return []
    return sorted((when, path) for path in paths if (when := made_at(path.name)) and path.is_file())


def _folders(db):
    """Where this database's backups are: the backups folder, then the old one in the data folder."""
    return [Path(db.backup_dir), db.path.parent / OLD_FOLDER_NAME]


# ── making one ──

def save(db, filename):
    """Copies the whole budget into a file called `filename` in the backups folder, complete or
    not at all, and returns its path. Falls back to the old folder in the data folder when the
    backups folder can't be written."""
    folders = _folders(db)
    for i, folder in enumerate(folders):
        try:
            return _copy_into(db, folder, filename)
        except (OSError, sqlite3.Error) as e:
            if i == len(folders) - 1:
                raise
            log.warning("Couldn't save a backup in %s (%s); saving it in %s", folder, e, folders[i + 1])


def _copy_into(db, folder, filename):
    folder.mkdir(parents=True, exist_ok=True)
    target, partial = folder / filename, folder / f".{filename}.partial"
    partial.unlink(missing_ok=True)
    try:
        with db.lock:
            db.conn.execute("VACUUM INTO ?", (str(partial),))  # one file, no -wal beside it
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def make(db, now=None):
    """A backup of the budget as it is now, remembering what it looked like (`changed`)."""
    with db.lock:  # nothing changes between the copy and remembering it
        target = save(db, name(now or datetime.now()))
        db.set_meta(DIGEST_KEY, digest(db))
    log.info("Backed up the budget to %s", target)
    return target


def digest(db):
    """A fingerprint of everything in the budget: every table and its rows, but not this
    session's undo (cleared at each launch) or the fingerprint kept in meta."""
    h = hashlib.sha256()
    with db.read() as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite%' ORDER BY name")]
        for table in tables:
            if table in NOT_THE_BUDGET:
                continue
            rows = c.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            h.update(repr((table, [d[0] for d in rows.description])).encode())
            for row in rows:
                if table == "meta" and row[0] == DIGEST_KEY:
                    continue
                h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def changed(db):
    """Whether the budget changed since the newest backup was made (or there's none)."""
    if not any(found(folder) for folder in _folders(db)):
        return True
    return db.get_meta(DIGEST_KEY) != digest(db)


# ── keeping them ──

def months_before(day, months):
    index = day.year * 12 + day.month - 1 - months
    year, month = index // 12, index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def prune(folder, today):
    """Deletes the backups in `folder` from before the last four months, except the newest one
    from before `today`; the side files of any backup that's gone; and copies left half made.
    Returns the backups it deleted."""
    folder = Path(folder)
    try:
        files = [path for path in folder.iterdir() if path.is_file()]
    except OSError:  # not there, or can't be read (Documents access refused)
        return []
    cutoff = months_before(today, KEPT_MONTHS)
    for path in files:  # only one copy of the app runs: no copy is being made now
        old_daily = _HALF_MADE.fullmatch(path.name)
        if _partial(path) or (old_daily and old_daily[1] < cutoff.isoformat()):
            path.unlink(missing_ok=True)
    backups = found(folder)
    before_today = [path for when, path in backups if when.date() < today]
    spare = before_today[-1] if before_today else None
    gone = []
    for when, path in backups:
        if when.date() < cutoff and path != spare:
            path.unlink(missing_ok=True)
            gone.append(path)
    for path in files:  # side files of the backups just deleted, or of ones deleted by hand
        main = _main_of(path)
        if main and (made_at(main.name) or _HALF_MADE.fullmatch(main.name)) and not main.exists():
            path.unlink(missing_ok=True)
    if gone:
        log.info("Deleted %d backup%s from before %s", len(gone), "" if len(gone) == 1 else "s", cutoff)
    return gone


def move_old(old, new):
    """Moves every file from the old backups folder into `new`, then removes the old folder.
    A backup takes along the side files beside it that hold anything (`_holds_something`);
    the others are dropped, like copies left half made. It can stop at any point and run again: a
    file leaves the old folder only once it's complete in the new one, and one already there
    isn't copied twice. Returns how many moved."""
    old, new = Path(old), Path(new)
    if not old.is_dir() or (new.exists() and old.samefile(new)):
        return 0
    new.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in sorted(old.iterdir()):  # a backup comes before its side files
        if path.name == ".DS_Store" or (path.is_file() and _partial(path)):
            path.unlink(missing_ok=True)
            continue
        if not path.is_file():  # a folder, or a side file that went with its backup
            continue
        if _main_of(path) and not _holds_something(path):
            path.unlink()
            continue
        target = new / path.name
        same = target.exists() and filecmp.cmp(path, target, shallow=False)  # moved before, stopped before deleting it here
        if target.exists() and not same:
            target = _free_name(target)
        _move_with_side_files(path, target)
        moved += not same
    if moved:
        log.info("Moved %d backup%s from %s to %s", moved, "" if moved == 1 else "s", old, new)
    try:
        old.rmdir()
    except OSError:
        log.warning("Left %s: something in it couldn't be moved", old)
    return moved


def _free_name(path):
    n = 2
    while (candidate := path.with_name(f"{path.stem} ({n}){path.suffix}")).exists():
        n += 1
    return candidate


def _holds_something(side):
    """Whether a side file holds anything: a -wal or -journal that isn't empty, or a -shm beside
    such a -wal. A -shm is only the -wal's index, rebuilt from it when it's needed, so without
    pages in the -wal it holds nothing, whatever its size."""
    if side.name.endswith("-shm"):
        side = side.with_name(side.name[:-len("-shm")] + "-wal")
    return side.is_file() and side.stat().st_size > 0


def _move_with_side_files(path, target):
    """Moves a backup with the side files beside it that hold anything, dropping the others.
    Side files go first: if this stops part way, the next try brings the backup to them, and
    if the backup itself can't be moved, they go back beside it."""
    sides = [path.with_name(path.name + suffix) for suffix in SIDE_FILES]
    keep = {side: _holds_something(side) for side in sides if side.is_file()}  # before an empty -wal goes
    moved = []
    try:
        for side, holds_something in keep.items():
            if not holds_something:
                side.unlink()
                continue
            there = target.with_name(target.name + side.name[len(path.name):])
            _put(side, there)
            moved.append((side, there))
        _put(path, target)
    except Exception:
        for side, there in moved:
            try:
                _move(there, side)
            except OSError:
                log.warning("Couldn't put %s back beside %s", there, path)
        raise


def _put(source, target):
    """Moves `source` to `target`, or only deletes it when the same file is already there."""
    if target.exists() and filecmp.cmp(source, target, shallow=False):
        source.unlink()
    else:
        _move(source, target)


def _move(source, target):
    try:
        os.rename(source, target)  # one step on the same disk
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        partial = target.with_name(f".{target.name}.partial")
        shutil.copy2(source, partial)
        os.replace(partial, target)
        source.unlink()


# ── launch and quit ──

def at_launch(db, now=None):
    """Once the database is open, before anything else changes it: moves the old backups over,
    backs up if the budget changed since the newest backup, and deletes the old ones. Each step
    is tried even if one before it failed. Returns the backup made, if any."""
    now = now or datetime.now()
    _try("Moving the old backups", lambda: move_old(db.path.parent / OLD_FOLDER_NAME, db.backup_dir))
    made = _try("Backing up at launch", lambda: make(db, now) if changed(db) else None)
    _prune_all(db, now)
    return made


def at_quit(db, now=None):
    """Every quit: a backup, then old ones go, then budget.db is made whole on its own (the
    write-ahead log folded in and emptied), so a backup copied back while the app is closed
    isn't mixed with what the log still held."""
    now = now or datetime.now()
    made = make(db, now)
    _prune_all(db, now)
    try:
        with db.lock:
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        log.exception("Couldn't fold the write-ahead log into the database")
    return made


def _prune_all(db, now):
    for folder in _folders(db):
        _try("Deleting old backups", lambda: prune(folder, now.date()))


def _try(what, step):
    """Runs one step of launching or quitting, so the next is tried even if this one fails. A
    folder that can't be written (Documents access refused) is one line in the log, not a
    traceback at every launch: save() has already used the data folder instead."""
    try:
        return step()
    except PermissionError as e:
        log.warning("%s failed: %s", what, e)
    except Exception:
        log.exception("%s failed", what)
    return None
