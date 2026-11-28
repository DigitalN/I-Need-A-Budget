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
- Each computer keeps a copy of the save it started from, and watches the folder. Another
  computer's newer save is taken straight away when this computer has changed nothing
  since; when both have, the budget was changed on two computers: the app shows what each
  changed, compared with that starting copy, and asks which to keep. The other becomes a
  backup. A sync tool's own conflict copies in the folder are treated the same way. Taking
  another computer's save clears ⌘Z, as an import does.
- The folder holds `Budget.inab`, `Backups/` (from every computer, kept four months as
  before) and `Imported Files/`. Each computer keeps its own settings, logs, working copy
  and starting copy. The budget's own settings (Overspending, Usual's months) were already
  in the budget (issue #153), so they travel with it.
- When the folder can't be reached, the app works on this computer's copy and saves when
  it's back.
- A computer on an older version refuses a budget saved by a newer one whose budget format
  it doesn't know, and offers the update.
- Asking at the update keeps ADR 0007: the person chooses, and nothing moves until they do.
- Restoring a backup stays copying it in (the maintainer's choice, 2026-11-27): quit the app on
  every computer and copy it into the folder as `Budget.inab`. No computer saved it, so it comes
  after whatever doesn't count it yet, and so does a save made on it: each computer with nothing
  of its own since its last save takes it, a laptop closed during the restore included, its own
  budget going into Backups/ first; one with changes not saved yet, or a save of its own in flight,
  asks. As a sync tool's conflict copy it's apart from the save it conflicts with, so it asks
  unless the contents are the same; Keep this over it comes after it, so the computers that took
  it take that. One copied into a folder the budget moved out of isn't taken: the move stands,
  and it goes into the new folder's Backups/.
- An older save put back in the folder (a sync tool's "restore previous version", of a restore
  saved back as itself too) is never saved over, nor gone back to, without asking: Use it, or Keep
  the newer one; the other goes into Backups/. Only this computer's own last save, when its newer
  one didn't land or sits beside it as the sync tool's copy, is saved over. The second without a
  question even if a person's "restore previous version" raced this computer's save: a sync tool
  makes a copy of one side of a conflict, never of a version a person goes back to, so the copy says
  the sync tool chose; nothing is lost (this computer's save came after the older one), and that
  version can be chosen again.
- Decided 2026-11-27, for 1.5.0.
