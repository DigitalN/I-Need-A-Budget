# SPDX-License-Identifier: GPL-3.0-only
"""The budget folder's rule, on random histories (as test_budget.py checks its invariant): three
computers, one budget folder, random sequences of changes, looks, a sync tool falling behind and
making conflict copies, backups restored (the same one twice, ones a computer missed), questions
answered either way, crashes mid-save, Open…, Move… and the folder going away. After every step:

- no change is lost: every change ever made is in a computer's budget or in a file the app keeps
  (Budget.inab, a sync tool's copy, Backups/, anywhere), or in what a restore copied over (the
  sync tool's version history);
- nothing asks when only one side changed: "changed on two computers" within one line is asked
  only when each side has a change the other hasn't.

Each change adds a category with a name of its own, so a budget's changes are its categories.
INAB_SEEDS=500 INAB_STEPS=60 runs more; a seed that ever found a bug stays in FOUND. Made-up data only."""

import os
import random
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import backups
import budget_folder
from budget_folder import FILE_NAME, BudgetFolder
from db import Database
from errors import UserError

# (seed, steps) that found a bug once, kept as they were: 12 made the budget folder again from a backup
# while it was unplugged; the others asked a computer whose other side held nothing new (a Move…'s save with
# the contents it started from, an older copy in its own line it had never had, a restore another computer
# went back to that it had had).
FOUND = [(12, 40), (46, 60), (61, 60), (80, 60), (104, 60), (114, 60), (150, 60), (156, 60), (161, 60),
         (185, 60), (188, 60), (267, 60), (282, 60), (299, 60), (305, 60)]
STEPS = int(os.environ.get("INAB_STEPS", "40"))
SEEDS = [(seed, STEPS) for seed in range(int(os.environ.get("INAB_SEEDS", "20")))] + FOUND
START = datetime(2026, 11, 27, 9, 0, tzinfo=timezone(timedelta(hours=-7)))


def categories(db):
    with db.read() as c:
        return {r[0] for r in c.execute("SELECT name FROM categories")}


def categories_in(path):
    try:
        with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as c:
            return {r[0] for r in c.execute("SELECT name FROM categories")}
    except sqlite3.Error:
        return set()


def add_category(db, name):
    with db.write("Add category") as c:
        group = c.execute("SELECT id FROM category_groups WHERE name = 'Everyday'").fetchone()
        group = group[0] if group else c.execute("INSERT INTO category_groups (name) VALUES ('Everyday')").lastrowid
        c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, name))


class Crash(BaseException):
    """The power going off in the middle of a save."""


class World:
    def __init__(self, tmp, seed):
        self.r, self.tmp, self.seed = random.Random(seed), tmp, seed
        self.t = START
        self.n = 0
        self.folder = tmp / "Budget"      # where the budget folder is now (the sync tool's own copy)
        self.made = set()                 # every change made
        self.pool = []                    # backups the person could restore
        self.lagging = {}                 # computer → (its view, Budget.inab's time and size in both when it fell behind)
        self.away = None                  # where the folder went while it can't be reached
        self.log = []
        self.computers = {}
        a = self._computer("PC-A")
        self._change(a, "Start")
        a.move_to(self.folder)
        for name in ("PC-B", "PC-C"):
            self._computer(name).open_from(self.folder)

    def _computer(self, name, db=None, folder=None):
        data = self.tmp / name
        c = BudgetFolder(db or Database(data / "budget.db"), data, folder, computer_name=name, app_version="1.5.0",
                         machine_id=f"machine-{name}", now=lambda: self.t, sleep=lambda s: None)
        self.computers[name] = c
        return c

    def _change(self, c, name):
        add_category(c.db, name)
        self.made.add(name)

    # ── what can happen ──

    def step(self):
        self.n += 1
        self.t += timedelta(minutes=1)
        op = self.r.choices(
            ["change", "look", "look", "lag", "sync", "backup", "restore", "answer", "crash", "reopen",
             "move", "follow", "unplug"],
            weights=[6, 6, 6, 2, 3, 1, 1, 3, 1, 1, 1, 2, 1])[0]
        name = self.r.choice(sorted(self.computers))
        self.log.append(f"{self.n} {op} {name}")
        getattr(self, op)(self.computers[name], name)
        self.check_the_rule()

    def change(self, c, name):
        self._change(c, f"N{self.n}")

    def look(self, c, name):
        c.check(wait=False)

    def lag(self, c, name):
        if name in self.lagging or self.away or c.folder is None or not c.folder.is_dir() or \
                not (self.folder / FILE_NAME).exists() or not _same(c.folder, self.folder):
            return
        view = self.tmp / f"{name} sees" / f"Budget {self.n}"
        shutil.copytree(self.folder, view)
        self.lagging[name] = (view, _key(self.folder / FILE_NAME), _key(view / FILE_NAME))
        c.folder, c._reachable = view, None

    def sync(self, c, name):
        if name not in self.lagging or self.away:
            return
        view, there, here = self.lagging.pop(name)
        if _key(view / FILE_NAME) not in (here, None):
            if _key(self.folder / FILE_NAME) != there:  # both changed: the sync tool keeps one, copies the other
                shutil.copyfile(view / FILE_NAME, self.folder / f"Budget-{name}.inab")
            else:
                shutil.copyfile(view / FILE_NAME, self.folder / FILE_NAME)
        for copy in view.glob("Budget-*.inab"):
            shutil.copyfile(copy, self.folder / copy.name)
        if c.folder == view:
            c.folder, c._reachable, c._seen = self.folder, None, {}

    def backup(self, c, name):
        made = backups.make(c.db)
        self.pool.append(shutil.copyfile(made, self.tmp / "pool" / f"{self.n}.db") if
                         (self.tmp / "pool").mkdir(exist_ok=True) is None else None)

    def restore(self, c, name):
        if not self.pool or self.away or not self.folder.is_dir():
            return
        main = self.folder / FILE_NAME
        if main.exists():  # what it copies over stays in the sync tool's version history
            (self.tmp / "history").mkdir(exist_ok=True)
            shutil.copyfile(main, self.tmp / "history" / f"{self.n}.inab")
        shutil.copyfile(self.r.choice(self.pool), main)

    def answer(self, c, name):
        if c.state()["state"] in ("two_computers", "older"):
            try:
                (c.keep_this if self.r.random() < 0.5 else c.keep_other)()
            except UserError:
                pass

    def crash(self, c, name):
        real = budget_folder._replace

        def cut(source, target, *args, **kwargs):
            if Path(target).name == FILE_NAME:
                raise Crash()
            return real(source, target, *args, **kwargs)

        budget_folder._replace = cut
        try:
            self._change(c, f"N{self.n}")
            c.check(wait=False)
        except Crash:
            pass
        finally:
            budget_folder._replace = real
        self._computer(name, db=c.db, folder=c.folder).at_launch()  # launched again

    def reopen(self, c, name):
        if c.folder is not None and c.folder.is_dir():
            try:
                c.open_from(c.folder)
            except UserError:
                pass

    def move(self, c, name):
        if self.lagging or self.away or c.state()["state"] != "saved" or not _same(c.folder, self.folder):
            return
        target = self.tmp / f"Moved {self.n}"
        try:
            c.move_to(target)
        except UserError:
            return
        self.folder = target

    def follow(self, c, name):
        if c.state()["state"] == "moved" and not self.away and name not in self.lagging:
            try:
                c.open_from(self.folder)
            except UserError:
                pass

    def unplug(self, c, name):
        if self.lagging:
            return
        if self.away is None and self.folder.is_dir():
            self.away = self.tmp / f"Unplugged {self.n}"
            self.folder.rename(self.away)
        elif self.away is not None:
            self.away.rename(self.folder)
            self.away = None

    # ── the rule ──

    def check_the_rule(self):
        kept = set()
        for c in self.computers.values():
            kept |= categories(c.db)
        for path in self.tmp.rglob("*"):
            if path.suffix in (".inab", ".db") and path.name != "budget.db" and "Saves" not in path.parts \
                    and "pool" not in path.parts and path.is_file():
                kept |= categories_in(path)
        lost = self.made - kept
        assert not lost, f"seed {self.seed}: {sorted(lost)} lost\n" + "\n".join(self.log)
        for name, c in self.computers.items():
            state, other, mine = c.state(), c._other, c._mine()
            if state["state"] != "two_computers" or other is None or mine is None or other.raw or \
                    other.line != mine.line or not (other.path and other.path.is_file()):
                continue
            theirs, ours = categories_in(other.path), categories(c.db)
            assert not theirs <= ours and not ours <= theirs, \
                f"seed {self.seed}: {name} asked though only one side changed\n" + "\n".join(self.log)


def _key(path):
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _same(a, b):
    return a is not None and b is not None and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@pytest.mark.parametrize("seed, steps", SEEDS)
def test_no_change_is_lost_and_nobody_is_asked_needlessly(tmp_path, seed, steps):
    world = World(tmp_path, seed)
    for _ in range(steps):
        world.step()
