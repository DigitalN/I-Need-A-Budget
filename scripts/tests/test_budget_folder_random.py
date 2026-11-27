# SPDX-License-Identifier: GPL-3.0-only
"""The budget folder's rule, on random histories (as test_budget.py checks its invariant): three
computers with clocks that disagree, one budget folder kept by a sync tool, and random sequences
of:

- changes (a category added, one deleted, a deleted one added back: changes back to contents
  once had);
- looks;
- a computer's sync falling behind, then syncing, where a conflict keeps either side and makes the
  other a copy, and a lagging computer's Backups/ arrives too;
- backups restored by hand, in the folder or in a lagging computer's copy of it (the same one
  twice, ones a computer missed);
- questions answered either way;
- crashes mid-save;
- Open…, Move… (with others lagging), and a computer's folder unplugged.

Every change is a version of the budget, with the version it was made on as its parent; a restore
is a version with none. The rule is checked on those versions, never on what a computer says:

- after every step, every version that nothing was made on since (a leaf) is still somewhere the
  app keeps it: a computer's budget, a budget file in the folder, or Backups/ (a lagging
  computer's copy of the folder, Saves/ and the person's own backups don't count), unless a
  person dropped it (a question's answer, a restore copied over it, or what a computer had when it
  took a restore, which must then be in Backups/);
- a question ("changed on two computers", "older") comes up only when neither side has everything
  the other has (or for the same backup restored again);
- at the end, once everything has synced and every question is answered, every computer has the
  same budget, every version is in its history or was dropped by a person, and every version an
  answer dropped is in Backups/: so a change both sides made was asked about.

INAB_SEEDS and INAB_STEPS run more (5,000+ locally); a seed that ever found a bug stays in FOUND.
Made-up data only."""

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
from budget_folder import FILE_NAME, MOVED_NOTE, BudgetFolder
from db import Database
from errors import UserError

NAMES = ("PC-A", "PC-B", "PC-C")
# (seed, steps) that found a bug once, in the app or in this test, kept as they were (without restores):
# 172 Move… made an unchanged budget a new save; 582 a copy put away after a move stayed in the old
# folder; 1559 a save found on following a move read as put back; 4045 the same contents joined read
# as older; 66, 1053, 1193, 1236, 1558 and 2442 this test's own mistakes.
FOUND = [(seed, 40) for seed in (66, 172, 582, 1053, 1193, 1236, 1558, 1559, 2442, 4045)]
STEPS = int(os.environ.get("INAB_STEPS", "40"))
SEEDS = [(seed, STEPS) for seed in range(int(os.environ.get("INAB_SEEDS", "100")))]
SEEDS += [found for found in FOUND if found not in SEEDS]
# The same, with backups restored by hand: 3, 45 and 54 following a move after a restore counted nothing
# known; 145 a change on top of a restore's very contents forgot what it was built on; 401 a computer that
# missed the restore of its very contents was asked; 2216 a budget saved again unchanged, and counts joined
# after a take, each counted one more of its own; 959 a save built on the same change made apart was asked
# about; 49, 60, 216, 763, 1053, 1120 and 3425 this test's own mistakes.
FOUND_WITH_RESTORES = [(seed, 40) for seed in (3, 45, 49, 54, 60, 145, 216, 401, 763, 959, 1053, 1120, 2216, 3425)]
RESTORE_SEEDS = [(seed, STEPS) for seed in range(int(os.environ.get("INAB_SEEDS", "100")))]
RESTORE_SEEDS += [found for found in FOUND_WITH_RESTORES if found not in RESTORE_SEEDS]
START = datetime(2026, 11, 27, 9, 0, tzinfo=timezone(timedelta(hours=-7)))


class Crash(BaseException):
    """The power going off in the middle of a save."""


def categories(db):
    with db.read() as c:
        return {r[0] for r in c.execute("SELECT name FROM categories")}


class World:
    def __init__(self, tmp, seed, restores=True):
        self.restores = restores  # backups restored by hand (copied in as Budget.inab)
        self.r, self.tmp, self.seed = random.Random(seed), tmp, seed
        self.t, self.n, self.log = START, 0, []
        self.skew = {name: timedelta(minutes=self.r.randint(-30, 30)) for name in NAMES}
        if self.r.random() < 0.2:  # one computer's clock months out
            self.skew[self.r.choice(NAMES)] += timedelta(days=self.r.choice((-200, 200)))
        self.cloud = tmp / "Budget"        # the folder as the sync tool keeps it
        self.home = {}                     # computer → the folder it syncs with (an old one after a move)
        self.lagging = {}                  # computer → (its copy of the folder, the folder, what both held then)
        self.unplugged = set()
        self.pool = []                     # backups the person made, to restore later
        self.digests = {}                  # file → ((time, size), digest)
        self.versions = []                 # (digest, parents)
        self.by_digest = {}                # digest → versions with it, oldest first
        self.children = {}
        self.dropped = set()               # versions a person dropped: answered against, or copied over
        self.answered = set()              # versions an answer dropped: they must be in Backups/
        self.saved = {}                    # a save's id → the version it was made from
        self.made = {}                     # a save's id → (its version, the save)
        self.choices = set()               # versions that are an answer: no contents of their own, only an order
        self.cur = {}                      # computer → the version its budget is
        self.base = {}                     # computer → the version its budget started from (taken, or saved)
        self.computers = {}
        a = self._computer("PC-A")
        add_category(a.db, "Start")
        a.move_to(self.cloud)
        for name in NAMES[1:]:
            self._computer(name).open_from(self.cloud)
        first = self._version(backups.digest(a.db), ())
        for name in NAMES:
            self.home[name] = self.cloud
            self.cur[name] = self.base[name] = first
            self._follow(name)

    def _computer(self, name, db=None, folder=None):
        data = self.tmp / name
        c = BudgetFolder(db or Database(data / "budget.db"), data, folder, computer_name=name, app_version="1.5.0",
                         machine_id=f"machine-{name}", now=lambda: self.t + self.skew[name], sleep=lambda s: None)
        self.computers[name] = c
        return c

    # ── versions ──

    def _version(self, digest, parents):
        v = len(self.versions)
        self.versions.append((digest, list(parents)))
        self.by_digest.setdefault(digest, []).append(v)
        for p in parents:
            self.children.setdefault(p, set()).add(v)
        return v

    def _digest_file(self, path):
        try:
            key = budget_folder._stat_key(path)  # its start too: one replaced within a clock tick, at the same size
            kept = self.digests.get(str(path))
            if kept is None or kept[0] != key:
                with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as c:
                    kept = (key, backups.digest_of(c))
                self.digests[str(path)] = kept
            return kept[1]
        except (OSError, sqlite3.Error):
            return None

    def _follow(self, name):
        """What the app did to a computer's budget (a take, Keep the other one): the version it is now."""
        d = backups.digest(self.computers[name].db)
        started = self.computers[name]._mine()  # the save its budget started from, and its contents
        taken = self.saved.get(started.id) if started is not None else None
        before = self.base[name]
        if taken is not None and taken != before and self.versions[taken][0] != d and \
                self.versions[taken][0] == self.versions[before][0]:
            # It adopted a save with the very contents it started from, its own change going on top of it.
            cur = self.cur[name]
            if taken not in self.versions[cur][1]:
                self.versions[cur][1].append(taken)
                self.children.setdefault(taken, set()).add(cur)
            self.base[name] = taken
        if taken is not None and self.versions[taken][0] == d:
            cur = self.cur[name]
            restore = started.id.startswith(budget_folder.RESTORED)
            if cur != taken and cur not in self._ancestry([taken]) and taken not in self._ancestry([cur]):
                if restore:
                    # It took a restore, whose history is its own from now (nothing it had is joined in): what it had,
                    # the person restored a backup over, kept in Backups/ first unless it's the very same contents
                    self.dropped.add(cur)
                    if self.versions[cur][0] != d:
                        self.answered.add(cur)
                elif self.versions[cur][0] == d:
                    # Its own change came to the very contents of another's save: the two are joined
                    self.versions[taken][1].append(cur)
                    self.children.setdefault(cur, set()).add(taken)
            self.cur[name] = taken  # its budget is that save exactly (taken, or the same contents adopted)
        elif d != self.versions[self.cur[name]][0]:
            assert d in self.by_digest, f"seed {self.seed}: {name}'s budget is contents nobody made\n" + self._trail()
            self.cur[name] = self.by_digest[d][-1]
        if started is not None and started.digest == self.versions[self.cur[name]][0]:
            cur = self.cur[name]
            if started.id not in self.saved:  # a save it just made: on what it started from, and on any save with
                # those very contents that it adopted on the way (another's Move…, a restore), which its counts come after
                restores = [(v, {f"{budget_folder.RESTORE}{rid[len(budget_folder.RESTORED):]}": 1})
                            for rid, v in self.saved.items() if rid.startswith(budget_folder.RESTORED)]
                for v, counts in [(v, kept.counts) for v, kept in self.made.values()] + restores:
                    if v not in (before, cur) and self.versions[v][0] == self.versions[before][0] and \
                            budget_folder._covers(started.counts, counts) and \
                            v not in self.versions[cur][1]:
                        self.versions[cur][1].append(v)
                        self.children.setdefault(v, set()).add(cur)
                self.made[started.id] = (cur, started)
            self.base[name] = cur
            self.saved.setdefault(started.id, cur)

    def _of_file(self, path):
        """The version a budget file in the folder is: the one its save was made from (its save id),
        or for a restored backup (no save in it) the restore of its contents."""
        try:
            with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as c:
                row = c.execute("SELECT value FROM meta WHERE key = 'save'").fetchone()
            kept = budget_folder.Save.from_kept(row[0]) if row and row[0] else None
        except (sqlite3.Error, OSError, ValueError, TypeError):
            kept = None
        if kept is not None and kept.id in self.saved:
            return self.saved[kept.id]
        d = self._digest_file(path)
        if kept is None and d is not None and f"{budget_folder.RESTORED}{d[:24]}" in self.saved:
            return self.saved[f"{budget_folder.RESTORED}{d[:24]}"]  # a backup copied in: the restore
        return self.by_digest[d][-1] if d in self.by_digest else None

    def _of_save(self, save):
        """The version a save is: by its id, else its file, else its contents."""
        if save.id in self.saved:
            return self.saved[save.id]
        if save.path is not None and Path(save.path).exists():
            return self._of_file(save.path)
        return self.by_digest[save.digest][-1] if save.digest in self.by_digest else None

    def _same_as(self, v, among):
        """Whether another version with `v`'s very contents, one `v` didn't come from (two computers
        making the same change), is `among` these: then what `v` holds is there too. One it came from
        doesn't count: a change back to contents once had is a change of its own."""
        came_from = self._ancestry([v])
        return any(u != v and u not in came_from and u in among for u in self.by_digest[self.versions[v][0]])

    def _with_the_same(self, versions):
        """`versions`, and every version with the very contents of one of them (made apart, or a restore
        of them), with what it came from: dropping those contents, or keeping them, is the same for it."""
        versions = set(versions)
        while True:
            more = {v for v in range(len(self.versions)) if v not in versions and self._same_as(v, versions)}
            if not more:
                return versions
            versions |= self._ancestry(more)

    def _ancestry(self, starts):
        seen, todo = set(), list(starts)
        while todo:
            v = todo.pop()
            if v not in seen:
                seen.add(v)
                todo.extend(self.versions[v][1])
        return seen

    # ── what can happen ──

    def step(self):
        self.n += 1
        self.t += timedelta(minutes=1)
        op = self.r.choices(
            ["add", "delete", "add_back", "look", "lag", "sync", "backup", "restore", "answer", "crash",
             "reopen", "move", "follow", "unplug"],
            weights=[4, 2, 1, 9, 2, 3, 1, 1 if self.restores else 0, 3, 1, 1, 1, 2, 1])[0]
        name = self.r.choice(NAMES)
        self.log.append(f"{self.n} {op} {name}")
        self.looked = None  # the computer the app looked for in this step (its question is what it found now)
        getattr(self, op)(self.computers[name], name)
        for each in NAMES:
            self._follow(each)
        self.check_the_rule(asked=self.looked)

    def _change(self, c, name, change):
        """A change: a new version made on this computer's, unless it goes straight back to the very
        budget this computer started from (then there's no change to lose)."""
        self._follow(name)
        change(c.db)
        d, back, before = backups.digest(c.db), self.base[name], self.cur[name]
        if d != self.versions[back][0]:
            self.cur[name] = self._version(d, (before,))
        elif before != back:  # its own change undone: in its history, and nothing to lose
            self.versions[back][1].append(before)
            self.children.setdefault(before, set()).add(back)
            self.cur[name] = back

    def add(self, c, name):
        self._change(c, name, lambda db: add_category(db, f"N{self.n}"))

    def delete(self, c, name):
        there = sorted(categories(c.db) - {"Start"})
        if there:
            gone = self.r.choice(there)
            self._change(c, name, lambda db: delete_category(db, gone))

    def add_back(self, c, name):
        gone = sorted({f"N{i}" for i in range(self.n)} - categories(c.db))
        if gone:
            back = self.r.choice(gone)
            self._change(c, name, lambda db: add_category(db, back))

    def look(self, c, name):
        self.looked = name
        c.check(wait=False)

    def _in_sync(self, name):
        return name not in self.lagging and name not in self.unplugged

    def lag(self, c, name):
        if not self._in_sync(name) or not _same(c.folder, self.home[name]) or not self.home[name].is_dir():
            return
        view = self.tmp / f"{name} sees" / f"Budget {self.n}"
        shutil.copytree(self.home[name], view)
        self.lagging[name] = (view, self.home[name], _snapshot(self.home[name]), _snapshot(view))
        c.folder, c._reachable, c._seen = view, None, {}
        c._point_backups()  # as the app does whenever its folder changes

    def sync(self, c, name):
        if name not in self.lagging:
            return
        view, folder, there, here = self.lagging.pop(name)
        now_there, now_here = _snapshot(folder), _snapshot(view)
        for rel in sorted(set(here) | set(now_here)):
            if not rel.endswith((".inab", ".db")):
                continue
            changed_here = now_here.get(rel) != here.get(rel)
            changed_there = now_there.get(rel) != there.get(rel)
            if not changed_here:
                continue
            if rel not in now_here:  # gone here (put away): gone there too, unless it changed there since
                if not changed_there and (folder / rel).exists():
                    (folder / rel).unlink()
            elif changed_there and rel == FILE_NAME and rel in now_there:
                if self.r.random() < 0.5:  # a conflict: the sync tool keeps one, makes the other a copy
                    shutil.copyfile(view / rel, folder / f"Budget-{name}.inab")
                else:
                    shutil.copyfile(folder / rel, folder / f"Budget-cloud-{self.n}.inab")
                    shutil.copyfile(view / rel, folder / rel)
            elif not changed_there or rel not in now_there:
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(view / rel, folder / rel)
        c.folder, c._reachable, c._seen = folder, None, {}
        c._point_backups()

    def backup(self, c, name):
        (self.tmp / "pool").mkdir(exist_ok=True)
        self.pool.append(shutil.copyfile(backups.make(c.db), self.tmp / "pool" / f"{self.n}.db"))

    def restore(self, c, name):
        """The person copies a backup in as Budget.inab: in the folder, or in this computer's copy of it."""
        folder = self.lagging[name][0] if name in self.lagging else self.home[name]
        if not self.pool or name in self.unplugged or not folder.is_dir() or (folder / MOVED_NOTE).exists():
            return
        if (folder / FILE_NAME).exists() and (over := self._of_file(folder / FILE_NAME)) is not None:
            self.dropped.add(over)  # what the person copied over, on purpose
        shutil.copyfile(self.r.choice(self.pool), folder / FILE_NAME)
        digest = self._digest_file(folder / FILE_NAME)
        self.saved[f"restored-{digest[:24]}"] = self._version(digest, ())  # what every computer makes of it

    def answer(self, c, name, keep_this=None):
        state = c.state()["state"]
        if state not in ("two_computers", "older") or c._other is None:
            return
        keep_this = self.r.random() < 0.5 if keep_this is None else keep_this
        self._follow(name)
        if keep_this:  # the other, and whatever else in the folder this one didn't come from, are dropped
            mine = self._ancestry([self.cur[name]])
            # What it was asked about (its file may be gone since: put away by the computer that has it)
            asked = [self._of_save(c._other)] + [self._of_file(p) for p in _budgets(c.folder)]
            dropping = {v for v in asked if v is not None and v not in mine}
            try:
                c.keep_this()
            except UserError:  # refused before it looked (the folder can't be reached), or it looked again
                return
            self.looked = name
            self.dropped |= dropping
            self.answered |= dropping
            # The choice is a step of its own, after both sides (its contents may be older than what it dropped)
            kept = self.cur[name]
            self.cur[name] = self._version(self.versions[kept][0], (kept, *dropping))
            self.choices.add(self.cur[name])
        else:
            before, known, asked = self.cur[name], set(self.saved), self._of_save(c._other)
            try:
                c.keep_other()
            except UserError:
                return
            self.looked = name
            self._follow(name)
            if self.cur[name] != before:
                self.dropped.add(before)
                self.answered.add(before)
                taken = self.cur[name]
                if asked is not None and self.versions[asked][0] == self.versions[taken][0]:
                    taken = asked  # what it was asked about (another version may have the same contents)
                self.cur[name] = choice = self._version(self.versions[taken][0], (taken, before))
                self.choices.add(choice)
                made = c._mine()
                if made is not None and made.id not in known:  # the save the answer made is the choice, not what it took
                    self.saved[made.id], self.made[made.id], self.base[name] = choice, (choice, made), choice

    def crash(self, c, name):
        real = budget_folder._replace

        def cut(source, target, *args, **kwargs):
            if Path(target).name == FILE_NAME:
                raise Crash()
            return real(source, target, *args, **kwargs)

        self.add(c, name)
        budget_folder._replace = cut
        try:
            c.check(wait=False)
        except Crash:
            pass
        finally:
            budget_folder._replace = real
        self.looked = name
        self._computer(name, db=c.db, folder=c.folder).at_launch()  # launched again

    def reopen(self, c, name):
        if self._in_sync(name) and c.folder is not None and c.folder.is_dir():
            try:
                c.open_from(c.folder)
                self.looked = name  # only once it's opened: refused (no budget there), it didn't look
            except UserError:
                pass

    def move(self, c, name):
        if not self._in_sync(name) or not _same(c.folder, self.cloud) or c.state()["state"] != "saved":
            return
        target = self.tmp / f"Moved {self.n}"
        self.looked = name
        try:
            c.move_to(target)
        except UserError:
            return
        self.cloud = self.home[name] = target

    def follow(self, c, name):
        if self._in_sync(name) and c.state()["state"] == "moved":
            try:
                c.open_from(self.cloud)
                self.looked = name
                self.home[name] = self.cloud
            except UserError:
                pass

    def unplug(self, c, name):
        if name in self.unplugged:
            self.unplugged.discard(name)
            c.folder, c._reachable = self.home[name], None
            c._point_backups()
        elif self._in_sync(name):
            self.unplugged.add(name)
            c.folder, c._reachable = self.tmp / "Unplugged" / name, None
            c._point_backups()

    # ── the rule ──

    def _kept(self):
        """The contents the app keeps: computers' budgets, budget files in the folder, Backups/ (the
        folder's, and the data folders' while one couldn't be reached)."""
        kept = {self.versions[self.cur[name]][0] for name in NAMES}
        places = [self.cloud, self.cloud / "Backups"] + [self.tmp / name / "Backups" for name in NAMES] + \
                 [self.tmp / name / backups.FOLDER_NAME for name in NAMES]
        for place in places:
            for path in place.glob("*") if place.is_dir() else []:
                if path.suffix in (".inab", ".db") and path.is_file() and not path.name.startswith("."):
                    kept.add(self._digest_file(path))
        return kept

    def check_the_rule(self, asked=None):
        """`asked`: the computer the app has just looked for (its question, if any, is what it
        found now; another's may be from before a change it made since, gone at its next look)."""
        kept = self._kept()
        for v, (digest, _) in enumerate(self.versions):
            if v not in self.children and v not in self.dropped and v not in self.choices and                     not self._same_as(v, self.children):
                assert digest in kept, f"seed {self.seed}: version {v} lost\n" + self._trail()
        for name, c in self.computers.items():
            if name != asked:
                continue
            state = c.state()["state"]
            if state in ("two_computers", "older") and c._other is not None:
                other = self._of_save(c._other)
                if other is None or (state == "older" and c._other.raw):
                    continue  # the same backup restored again, older by now: asked (decided on #196's review)
                mine = self.cur[name]
                assert other not in self._ancestry([mine]) and mine not in self._ancestry([other]), \
                    f"seed {self.seed}: {name} asked ({state}) though one side has everything: it's v{mine}, " \
                    f"{c._other.name} v{other}\n" + self._trail()

    def settle(self):
        """Everything syncs, every computer looks until none is asked, and the person answers each
        question by keeping the budget in front of them."""
        for name in sorted(self.unplugged):
            self.unplug(self.computers[name], name)
        for name in sorted(self.lagging):
            self.sync(self.computers[name], name)
        states = {}
        for _ in range(12):
            for name in NAMES:
                c = self.computers[name]
                state = c.check(wait=False)["state"]
                for each in NAMES:
                    self._follow(each)
                self.check_the_rule(asked=name)
                if state == "moved":
                    self.follow(c, name)
                elif state in ("two_computers", "older"):
                    self.answer(c, name, keep_this=True)
                for each in NAMES:
                    self._follow(each)
                self.check_the_rule()
            states = {name: self.computers[name].check(wait=False)["state"] for name in NAMES}
            for each in NAMES:
                self._follow(each)
            if set(states.values()) == {"saved"} and len({self.versions[self.cur[n]][0] for n in NAMES}) == 1:
                break
        final = {self.versions[self.cur[n]][0] for n in NAMES}
        assert set(states.values()) == {"saved"} and len(final) == 1, \
            f"seed {self.seed}: never agree: {states}\n" + self._trail()
        history = self._with_the_same(self._ancestry(self.by_digest[final.pop()]))
        dropped = self._with_the_same(self._ancestry(self.dropped))
        for v in range(len(self.versions)):
            assert v in history or v in dropped or v in self.choices, \
                f"seed {self.seed}: version {v} is in no budget and nobody chose to drop it\n" + self._trail()
        kept = self._kept()
        for v in self.answered:
            assert self.versions[v][0] in kept, \
                f"seed {self.seed}: version {v}, answered against, isn't in Backups/\n" + self._trail()

    def _trail(self):
        return "\n".join(self.log[-40:])


def add_category(db, name):
    with db.write("Add category") as c:
        group = c.execute("SELECT id FROM category_groups WHERE name = 'Everyday'").fetchone()
        group = group[0] if group else c.execute("INSERT INTO category_groups (name) VALUES ('Everyday')").lastrowid
        c.execute("INSERT INTO categories (group_id, name) VALUES (?, ?)", (group, name))


def delete_category(db, name):
    with db.write("Delete category") as c:
        c.execute("DELETE FROM categories WHERE name = ?", (name,))


def _budgets(folder):
    try:
        return [p for p in Path(folder).iterdir() if p.suffix == ".inab" and not p.name.startswith(".") and p.is_file()]
    except OSError:
        return []


def _snapshot(folder):
    """What a folder holds: each file's time, size and first bytes, by its path in the folder."""
    out = {}
    for path in Path(folder).rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            stat = path.stat()
            with open(path, "rb") as f:
                out[path.relative_to(folder).as_posix()] = (stat.st_mtime_ns, stat.st_size, f.read(1 << 16))
    return out


def _same(a, b):
    return a is not None and b is not None and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def run(tmp, seed, steps, restores=True):
    world = World(tmp, seed, restores)
    try:
        for _ in range(steps):
            world.step()
        world.settle()
    finally:
        for c in world.computers.values():
            try:
                c.db.close()
            except Exception:
                pass
    return world


@pytest.mark.parametrize("seed, steps", SEEDS)
def test_no_change_is_lost_and_nobody_is_asked_needlessly(tmp_path, seed, steps):
    """Everything but backups restored by hand: what holds however restores end up working."""
    run(tmp_path, seed, steps, restores=False)


@pytest.mark.parametrize("seed, steps", RESTORE_SEEDS)
def test_backups_restored_by_copying_them_in_too(tmp_path, seed, steps):
    """Everything, backups restored by hand included (copied in as Budget.inab: the maintainer's
    choice on #196's fifth review)."""
    run(tmp_path, seed, steps, restores=True)
