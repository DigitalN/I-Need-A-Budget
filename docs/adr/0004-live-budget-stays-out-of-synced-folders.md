# The live budget stays out of synced folders; only backups go there

The maintainer wants the budget backed up off the computer, by iCloud Drive (or OneDrive),
which sync the Documents folder. The live database stays where it was, in Application
Support: it's written as you work (SQLite in WAL mode, so three files), and a sync can
catch it halfway and restore a damaged or out-of-date copy. Two computers on one synced
folder would also open the same budget at once and split it into conflict copies,
quietly losing one side's work. So only backups, complete copies made when the app
quits, go to `Documents/I Need A Budget Backups`.

## Consequences

- A backup is made at every quit, and at launch only when the budget changed since the
  newest backup (the last session didn't quit normally: a crash, a power cut).
- Backups, the copies made before a database upgrade included, are kept four months. The
  newest backup from before today is never deleted for its age, so a long break can't
  empty the folder.
- One budget per copy of the app: nothing syncs a budget between computers. Moving one
  to another computer means copying a backup across.
- The backups that were in Application Support's `Backups/` folder are moved to the new
  one once. If Documents can't be written (access refused in System Settings), backups
  go there instead until it can.
- Restoring one is copying it back as `budget.db` while the app is closed. Quitting folds
  the write-ahead log into `budget.db` and empties it, so after a normal quit nothing
  else in the data folder still holds changes; after a crash, `budget.db-wal` and
  `budget.db-shm` are deleted first.
- Decided 2026-10-14, for Windows and the Mac; built for the Mac alone in 1.3.1 once the
  Windows plan was shelved. Backups used to be made at launch, one per day, 14 kept.
