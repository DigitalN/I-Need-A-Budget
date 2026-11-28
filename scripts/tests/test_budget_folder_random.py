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
is a version after what it was copied over; and what the app makes of saves (a take, the same
contents joined, an answer) is an event too, noted as it happens. The rules are checked on those
versions, never on what a computer says:

- after every step, every version that nothing was made on since (a leaf) is still somewhere the
  app keeps it: a computer's budget, a budget file in the folder, or Backups/ (a lagging
  computer's copy of the folder, Saves/ and the person's own backups don't count), unless a
  person dropped it (a question's answer, a restore copied over it);
- a question ("changed on two computers", "older") is needless, and fails, when both sides have
  the same contents, or when it's about a Budget.inab that has everything this computer's last save
  or take has and nothing changed here (a backup copied in asks, by design: an old save);
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
# as older; 306 and 334 Keep this asked again forever about a third computer's save; 66, 1053, 1193,
# 1236, 1558, 1758 and 2442 this test's own mistakes.
FOUND = [(seed, 40) for seed in (66, 172, 306, 334, 582, 1053, 1193, 1236, 1558, 1559, 1758, 2442, 4045)]
STEPS = int(os.environ.get("INAB_STEPS", "40"))
SEEDS = [(seed, STEPS) for seed in range(int(os.environ.get("INAB_SEEDS", "100")))]
SEEDS += [found for found in FOUND if found not in SEEDS]
# The same, with backups restored by hand: 3, 45 and 54 following a move after a restore counted nothing
# known; 145 a change on top of a restore's very contents forgot what it was built on; 401 a computer that
# missed the restore of its very contents was asked; 2216 a budget saved again unchanged, and counts joined
# after a take, each counted one more of its own; 959 and 51662 a save built on the same change made apart
# was asked about; 48 and 202 a save in flight (left in the old folder, its copy put away) was taken over by
# a restore unasked; 384 one with nothing of its own was asked after following a move; 50173 an older
# Budget.inab beside this computer's own newer copy, and a restore hidden by a save of the same contents;
# 60106 (80 steps) a change back to a restore's contents overridden (the sixth review's); 50057 a save of
# its own left as a copy in the old folder wasn't brought; 51413 and 60214 (80) a copy its own change came
# after was asked about; 50017 a save joined with this computer's kept the other's id; 72024 (80, the seventh
# review's), 1532 (80) and 1567 (80) a change not saved yet folded into a join (the seventh review's cause, found
# here once this test made the same change on two computers and kept some unsaved a while), 589 a copy of its own
# from before such a join asked about, 74006 (80) a restore saved back as itself taken for a save in flight;
# 39, 49, 60, 94, 101, 158, 216, 279, 763, 1023 (80), 1053, 1120, 1868, 3425, 50947, 51482, 60169 (80) and 60189
# (80) this test's own mistakes. (Since the seventh review a seed's changes are drawn apart from its steps, so the
# earlier seeds' histories differ from when they found their bugs: they stay as cases all the same.) On the five
# rules: 46 a question they ask by design (an answer made on a restore can't count what it was copied over).
FOUND_WITH_RESTORES = [(seed, 40) for seed in (3, 39, 45, 46, 48, 49, 54, 60, 94, 101, 145, 158, 202, 216, 279, 384,
                                               401, 589, 763, 959, 1053, 1120, 1868, 2216, 3425, 50017, 50057,
                                               50173, 50947, 51413, 51482, 51662)] + \
    [(seed, 80) for seed in (1023, 1532, 1567, 60106, 60169, 60189, 60214, 72024, 74006)]
RESTORE_SEEDS = [(seed, STEPS) for seed in range(int(os.environ.get("INAB_SEEDS", "100")))]
RESTORE_SEEDS += [found for found in FOUND_WITH_RESTORES if found not in RESTORE_SEEDS]
START = datetime(2026, 11, 27, 9, 0, tzinfo=timezone(timedelta(hours=-7)))


class Crash(BaseException):
    """The power going off in the middle of a save."""


def _free(folder, stem):
    """A conflict copy's name the sync tool hasn't used yet: it never overwrites one of its own."""
    path, n = folder / f"{stem}.inab", 2
    while path.exists():
        path, n = folder / f"{stem}-{n}.inab", n + 1
    return path


def categories(db):
    with db.read() as c:
        return {r[0] for r in c.execute("SELECT name FROM categories")}


class World:
    def __init__(self, tmp, seed, restores=True):
        self.restores = restores  # backups restored by hand (copied in as Budget.inab)
        self.r, self.tmp, self.seed = random.Random(seed), tmp, seed
        self.r2 = random.Random(seed * 7919 + 1)  # what a change is, apart from the steps: the same steps either way
        self.added = {}                    # computer → the categories it added, in order
        self.last = {}                     # computer → (its budget's contents before its last change, the change)
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
        self.saved = {}                    # a save's id → the version it is (a save, a join, an answer)
        self.raw = {}                      # a backup's contents → the newest restore of it (copied in)
        self.choices = set()               # versions that are an answer: no contents of their own, only an order
        self.restored = set()              # backups copied in: the person's own files, never the app's to keep
        self.restored_into = {}            # a backup copied in → the budget folder it went into (through a lagging copy)
        self.took = {}                     # computer → what the app did since it was last followed, as it happened
        self.cur = {}                      # computer → the version its budget is
        self.base = {}                     # computer → the version of its last save or take
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
        take, join, answer = c._take, c._join, c._answer

        def took(save, *args, **kwargs):  # rule 2 (or Use it, or another budget opened): the save it took
            done = take(save, *args, **kwargs)
            if done:
                self.took.setdefault(name, []).append(("take", (save.id, save.digest, save.raw)))
            return done

        def joined(saves, *args):  # rule 3: the saves with its budget's very contents, and the event it made
            made = join(saves, *args)
            self.took.setdefault(name, []).append(("join", [(s.id, s.digest, s.raw) for s in saves], made.id, made.digest))
            return made

        def answered(other, before):  # rule 4's answer: the event it made (`answer` models it)
            made = answer(other, before)
            self.took.setdefault(name, []).append(("answer", other.id, made.id))
            return made

        c._take, c._join, c._answer = took, joined, answered
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
        """What the app did to a computer's budget, as it happened (a take: its budget is that save; the
        same contents joined: an event after those saves and what it had), then a save it made of it."""
        for event in self.took.pop(name, []):
            assert event[0] != "answer", f"seed {self.seed}: an answer nobody gave\n" + self._trail()
            if event[0] == "take":
                got = self._of_id(*event[1])
                if got is not None:
                    self.cur[name] = self.base[name] = got
            else:
                _, saves, made, digest = event
                cur = self.cur[name]
                assert self.versions[cur][0] == digest, f"seed {self.seed}: {name} joined other contents\n" + self._trail()
                # Only a save with these very contents comes into the event (the app saying it includes one that
                # doesn't have them would hide a change: left out here, it's found lost)
                same = [v for v in (self._of_id(*s) for s in saves) if v is not None and self.versions[v][0] == digest]
                self.saved[made] = self.cur[name] = self.base[name] = \
                    self._version(digest, list(dict.fromkeys([cur] + same)))
        c = self.computers[name]
        d = backups.digest(c.db)
        if d != self.versions[self.cur[name]][0]:
            assert d in self.by_digest, f"seed {self.seed}: {name}'s budget is contents nobody made\n" + self._trail()
            self.cur[name] = self.by_digest[d][-1]
        started = c._mine()
        if started is not None:
            if started.id not in self.saved and started.digest == self.versions[self.cur[name]][0]:
                self.saved[started.id] = self.cur[name]  # a save it just made of its budget
            if started.id in self.saved:
                self.base[name] = self.saved[started.id]

    def _of_id(self, sid, digest, raw):
        """The version a save the app read is: a backup copied in by its contents, else by its id."""
        if raw:
            return self.raw.get(digest)
        return self.saved.get(sid, self.by_digest[digest][-1] if digest in self.by_digest else None)

    def _of_file(self, path):
        """The version a budget file in the folder is: the one its save is (its save id), or for a
        backup copied in (no save in it) the restore of its contents."""
        try:
            with closing(sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True)) as c:
                row = c.execute("SELECT value FROM meta WHERE key = 'save'").fetchone()
            kept = budget_folder.Save.from_kept(row[0]) if row and row[0] else None
        except (sqlite3.Error, OSError, ValueError, TypeError):
            kept = None
        d = self._digest_file(path)
        if d is None:
            return None
        return self._of_id(kept.id if kept else None, d, kept is None)

    def _of_save(self, save):
        """The version a save is: by its id, else its contents."""
        return self._of_id(save.id, save.digest, save.raw)

    def _ancestry(self, starts, without=()):
        """Every version `starts` came from; not through the parents of those `without`."""
        seen, todo = set(), list(starts)
        while todo:
            v = todo.pop()
            if v not in seen:
                seen.add(v)
                if v not in without:
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

    def _change(self, c, name, change, what=None):
        """A change: a new version made on this computer's, unless it goes straight back to the very
        budget this computer started from (then there's no change to lose). `what`: (add or delete, the
        category), for another computer to make the very same change (`_mirror`)."""
        self._follow(name)
        if what is not None:
            self.last[name] = (backups.digest(c.db), what)
        change(c.db)
        d, back, before = backups.digest(c.db), self.base[name], self.cur[name]
        if d != self.versions[back][0]:
            self.cur[name] = self._version(d, (before,))
        elif before != back:  # its own change undone: in its history, and nothing to lose
            self.versions[back][1].append(before)
            self.children.setdefault(before, set()).add(back)
            self.cur[name] = back

    def _mirror(self, c, name):
        """The very change another computer made from the very budget this one has: made here too, not
        saved yet, so it comes to the other's contents (a join of a change not saved yet). True if made."""
        d = backups.digest(c.db)
        made = [(other, change) for other, (before, change) in self.last.items() if other != name and before == d]
        if not made or self.r2.random() >= 0.5:
            return False
        other, (kind, category) = self.r2.choice(made)
        if (kind == "add") == (category in categories(c.db)):
            return False
        self._change(c, name, lambda db: (add_category if kind == "add" else delete_category)(db, category), (kind, category))
        return True

    def add(self, c, name):
        if self._mirror(c, name):
            return
        new = f"N{self.n}"
        made_elsewhere = [n for other in NAMES if other != name for n in self.added.get(other, [])[-2:]
                          if n not in categories(c.db)]
        if made_elsewhere and self.r2.random() < 0.5:  # the same change another computer made: a join to come
            new = self.r2.choice(made_elsewhere)
        self.added.setdefault(name, []).append(new)
        self._change(c, name, lambda db: add_category(db, new), ("add", new))

    def delete(self, c, name):
        there = sorted(categories(c.db) - {"Start"})
        if there:
            gone = self.r.choice(there)
            mine = [n for n in self.added.get(name, []) if n in there]
            if mine and self.r2.random() < 0.6:  # its own latest change taken back: its budget as it was before
                gone = mine[-1]
            if self._mirror(c, name):
                return
            self._change(c, name, lambda db: delete_category(db, gone), ("delete", gone))

    def add_back(self, c, name):
        gone = sorted({f"N{i}" for i in range(self.n)} - categories(c.db))
        if gone:
            back = self.r.choice(gone)
            self.added.setdefault(name, []).append(back)
            self._change(c, name, lambda db: add_category(db, back), ("add", back))

    def look(self, c, name):
        self.looked = name
        c.check(wait=self.r2.random() < 0.5)  # the watcher's look waits for changes to stop: some stay unsaved a while

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
                    shutil.copyfile(view / rel, _free(folder, f"Budget-{name}"))
                else:
                    shutil.copyfile(folder / rel, _free(folder, f"Budget-cloud-{self.n}"))
                    shutil.copyfile(view / rel, folder / rel)
            elif not changed_there or rel not in now_there:
                (folder / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(view / rel, folder / rel)
        c.folder, c._reachable, c._seen = folder, None, {}
        c._point_backups()

    def backup(self, c, name):
        (self.tmp / "pool").mkdir(exist_ok=True)
        self.pool.append(shutil.copyfile(backups.make(c.db, c._clock_time()), self.tmp / "pool" / f"{self.n}.db"))  # by its clock, as the app

    def restore(self, c, name):
        """The person copies a backup in as Budget.inab: in the folder, or in this computer's copy of it."""
        folder = self.lagging[name][0] if name in self.lagging else self.home[name]
        if not self.pool or name in self.unplugged or not folder.is_dir() or (folder / MOVED_NOTE).exists():
            return
        over = self._of_file(folder / FILE_NAME) if (folder / FILE_NAME).exists() else None
        if over is not None:
            self.dropped.add(over)  # what the person copied over, on purpose
        shutil.copyfile(self.r.choice(self.pool), folder / FILE_NAME)
        digest = self._digest_file(folder / FILE_NAME)
        # After what the folder held (copied over on purpose), and after the same backup restored before
        again = self.raw.get(digest)
        parents = [v for v in (over, again) if v is not None]
        self.raw[digest] = v = self._version(digest, list(dict.fromkeys(parents)))
        self.restored.add(v)
        self.restored_into[v] = self.lagging[name][1] if name in self.lagging else folder

    def answer(self, c, name, keep_this=None):
        state = c.state()["state"]
        if state not in ("two_computers", "older") or c._other is None:
            return
        keep_this = self.r.random() < 0.5 if keep_this is None else keep_this
        self._follow(name)
        other, before = c._other, self.cur[name]
        asked = self._of_save(other)
        if keep_this:  # the side shown is dropped, and any budget in the folder with its very contents (what the person saw)
            mine = self._ancestry([before])
            shown = [asked] + [v for v in (self._of_file(p) for p in _budgets(c.folder))
                               if v is not None and other.digest == self.versions[v][0]]
            dropping = {v for v in shown if v is not None and v not in mine}
        try:
            c.keep_this() if keep_this else c.keep_other()
        except UserError:  # refused before it looked (the folder can't be reached, moved since)
            return
        self.looked = name
        events = self.took.pop(name, [])
        made = [i for i, e in enumerate(events) if e[0] == "answer"]
        assert len(made) == 1, f"seed {self.seed}: {name} answered {len(made)} times\n" + self._trail()
        first, answer, after = events[:made[0]], events[made[0]][2], events[made[0] + 1:]
        if keep_this:
            assert not first, f"seed {self.seed}: {name} took something keeping its own\n" + self._trail()
            self.dropped |= dropping
            # In Backups/ after: what it dropped (unless it's the very contents kept: nothing of it is gone)
            self.answered |= {v for v in dropping if self.versions[v][0] != self.versions[before][0]}
            choice = self._version(self.versions[before][0], (before, *dropping))  # a step after both sides
        else:
            assert [e[0] for e in first] == ["take"] and first[0][1][0] == other.id and asked is not None, \
                f"seed {self.seed}: {name} didn't take what it was asked about\n" + self._trail()
            self.dropped.add(before)
            if self.versions[before][0] != self.versions[asked][0]:  # the very contents kept: nothing of it is gone
                self.answered.add(before)
            choice = self._version(self.versions[asked][0], (asked, before))
        self.choices.add(choice)
        self.saved[answer] = self.cur[name] = self.base[name] = choice
        self.took[name] = after  # what its look after the answer did: after the choice
        self._follow(name)

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
            if v not in self.children and v not in self.dropped and v not in self.choices and v not in self.restored:
                assert digest in kept, f"seed {self.seed}: version {v} lost\n" + self._trail()
        for name, c in self.computers.items():
            if name != asked:
                continue
            state = c.state()["state"]
            if state in ("two_computers", "older") and c._other is not None:
                other, mine, base = self._of_save(c._other), self.cur[name], self.base[name]
                assert c._other.digest != self.versions[mine][0], \
                    f"seed {self.seed}: {name} asked ({state}) though both sides are the same: v{mine}, " \
                    f"{c._other.name} v{other}\n" + self._trail()
                # By design (accepted on the rebuild): a backup copied in counts nothing, so an answer made on it
                # never counts what it was copied over: a computer that had only that is asked once more.
                if c._other.name == FILE_NAME and other is not None and other not in self.restored and \
                        base not in self._ancestry([other]) - self._ancestry([other], self.restored):
                    assert not (mine == base and other != base and base in self._ancestry([other])), \
                        f"seed {self.seed}: {name} asked ({state}) though Budget.inab v{other} has everything its " \
                        f"save v{base} has and nothing changed here\n" + self._trail()

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
        history = self._ancestry({self.cur[n] for n in NAMES})  # events: what the computers' budgets came from
        dropped = self._ancestry(self.dropped)
        kept_contents, kept = {self.versions[u][0] for u in history}, self._kept()
        # A backup copied in is the person's file, not work: outside the history only when it changed nothing (the
        # very contents of what's kept: the same backup restored over what it already was), or when it went into a
        # folder the budget moved out of (the move stands: it's in Backups/); with what led into it (a change undone)
        aside = self._ancestry({v for v in self.restored if v not in history and (
            self.versions[v][0] in kept_contents or
            ((self.restored_into[v] / MOVED_NOTE).exists() and self.versions[v][0] in kept))})
        for v in range(len(self.versions)):
            assert v in history or v in dropped or v in self.choices or v in aside, \
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
