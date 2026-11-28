# The budget lives in a folder you choose, saved there whole

So that one budget can be used on a Mac and on a Windows PC, it lives in a budget folder the
person chooses, and their own tool (OneDrive, iCloud Drive, Dropbox, a drive of their own)
syncs it and backs it up: the app never goes online for it. Choosing the folder is
required: a new budget asks where at onboarding, and a copy updating to 1.5.0 asks once,
before anything else, and moves its budget there. This replaces ADR 0004, whose two dangers
still hold, so the folder never holds a live database: each computer works on its own copy
in its data folder, and the app saves the budget into the folder whole (a complete file
under a hidden name, then renamed) a few seconds after changes stop, and when it quits.

## Considered options

- The live SQLite files (`budget.db`, `-wal`, `-shm`) in the synced folder: the least code,
  but a sync tool can upload them halfway through a change or out of step with each other,
  and two computers with it open make conflict copies that quietly lose one side's work.
- Open on two computers at once with their changes merged (Actual Budget's sync): undo, the
  inbox and "what you decided stays decided" would all have to merge. A project of its own.

## Consequences

- One computer at a time. While the budget is open, the app leaves a note in the folder
  (which computer, since when); opening it elsewhere warns, and you can carry on. A sync
  tool takes a while to deliver the note, so it's a heads-up, not a lock.
- Each computer keeps a copy of the save it started from, and watches the folder. Saves are
  ordered by counts (each computer's count of its own saves), never by a clock, and conflicts
  follow five rules, the only ones (decided 2026-11-27, replacing the earlier, longer set). A save
  in the folder is `Budget.inab` or a sync tool's conflict copy of it; one *includes* another when
  its counts are at least the other's, and when they're equal only if it's the same save (the
  same id). Every new save counts more than what it came from, so equal counts with another id
  only come of counts used twice (a data folder and the budget folder both put back to an earlier
  day), and that asks (decided on #196's eighth review).
  1. A conflict copy that `Budget.inab` includes, or with its very contents, or that this
     computer's last save or take includes, goes into Backups/ (deleted when Backups/ has its
     contents). Silently. Its contents being in Backups/ settle nothing: a backup isn't a
     decision (every quit backs up before it saves), so such a copy asks under rule 4.
  2. `Budget.inab` is taken silently only when it includes this computer's last save or take and
     nothing changed here since; this computer's budget goes into Backups/ first unless its
     contents are there. Taking it clears ⌘Z, as an import does.
  3. A save whose contents are exactly this computer's budget asks nothing: this computer's save
     becomes a new event after both (their counts merged, plus one of its own). Nothing else is
     taken in that look.
  4. Everything else asks, one question about two sides: this computer's budget and one other
     save, `Budget.inab` first. "The budget in the folder is older than this one" when this
     computer's counts include the other's, otherwise "This budget changed on two computers",
     shown with what each changed. Use it or Keep this: the side that loses goes into Backups/
     first, and the answer is a new event of this computer's (both counts merged, plus one of its
     own), so the other computer takes it under rule 2. Whatever arrived that wasn't one of the
     two sides shown isn't counted by the answer: it's asked about next.
  5. This computer writes `Budget.inab` only when nothing in the folder is left unsettled by rules
     1–3; otherwise it asks first, keeping its change meanwhile.
- What counts as a change, and as the same contents (rules 2, 3 and 1), is `backups.digest`. It
  leaves out what's kept about saves and backups, and what only shows the budget, which a launch
  on a settled budget changes on its own (decided on #196's eighth review): an inbox item's reason
  (only text to show, written again by the re-check) and a folded group (how this computer shows
  the Budget page). A change of only those is saved and backed up with the next real one; a take
  replaces them with the other computer's.
- Restoring a backup stays copying it in (the maintainer's choice, 2026-11-27): quit the app on
  every computer and copy it into the folder as `Budget.inab`. A backup keeps nothing about saves,
  so it's an old save: older than every computer's, it asks (Use it).
- Accepted costs of keeping to five rules (decided 2026-11-27):
  - After a restore, the first computer opened asks once (older: Use it), and the others take its
    answer. If two open before the sync finishes, both ask, and rule 1 or 3 settles the two
    identical answers.
  - A backup copied in while another computer saved can become the sync tool's conflict copy;
    rule 1 then puts it into Backups/ as an older save, so it must be copied in again.
  - Two computers that made different changes always ask.
  - An answer made on a restored backup counts only what that computer had (the random test found
    it): a computer that had only the save the backup was copied over is asked once more.
  - Move… leaves another computer's saves from the old folder in the new folder's Backups/, and
    its question waits until that computer follows the move.
  - A sync tool delivering a new `Budget.inab` between the check and the rename is overwritten;
    the sync tool's version history keeps it.
  - A computer opened before the other's last save has synced, whose launch re-check categorizes
    what One at a time left, asks once (its side shows what the app categorized).
- The folder holds `Budget.inab`, `Backups/` (from every computer, kept four months as
  before) and `Imported Files/`. Each computer keeps its own settings, logs, working copy
  and starting copy. The budget's own settings (Overspending, Usual's months) were already
  in the budget (issue #153), so they travel with it.
- When the folder can't be reached, the app works on this computer's copy and saves when
  it's back.
- A computer on an older version refuses a budget saved by a newer one whose budget format
  it doesn't know, and offers the update.
- Asking at the update keeps ADR 0007: the person chooses, and nothing moves until they do.
- Decided 2026-11-27, for 1.5.0.
