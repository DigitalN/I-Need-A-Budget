# An update never changes a setting a copy uses

1.0.5 added the Overspending setting with This month as its default. A copy whose
`settings.json` had no value for it then ran on This month, where until then it had paid
for overspending like Actual Budget (Next month): its Budget page changed without a word.
On 2026-11-12 the maintainer, finding that assigning to a category already overspent in
cash left Ready to assign as it was (This month had taken it out already), asked for Next
month to be the default again, and for an update never to change a setting again (issue
#166).

So a default is only for something new: `settings.DEFAULTS` for a new copy of the app,
`settings.BUDGET_DEFAULTS` for a new budget. Everything that already exists keeps what it
was using.

## Considered options

- Change the default and let copies without a saved value follow it, as 1.0.5 did: that's
  what went wrong.
- Write every setting into `settings.json` when it's first made, and nothing else: a copy
  made before a setting existed has no value for it, and still follows whatever default
  the code has then.

## Consequences

- The first time a copy launches 1.4.1, its `settings.json` is written out whole with the
  values it was using: a value it had stays, and one it had no value for takes what that
  copy ran on (`settings.RAN_ON`, `settings.BUDGET_RAN_ON`: This month for Overspending, or
  Carry over when 1.0.3's switch was on), never a newer default. A setting added later
  goes into `RAN_ON` with the value that keeps what copies did before it existed, and
  `RAN_ON` never changes.
- In the same launch, Overspending and Usual's months move into the budget itself (its
  `meta`, issue #153), so a backup carries them and changing one counts as a change for
  backups. A new budget starts with Next month and 3 months. `settings.json` keeps what
  belongs to this Mac (Appearance, Import from Downloads and its folder, updates, the
  Dashboard's months) and, under `moved_to_the_budget`, what this Mac's budget ran on when
  the two moved: a backup from before 1.4.1 restored later takes those, and on a Mac that
  never ran an older version, what a copy without them ran on (This month, 3).
- Changing a default reaches only new copies and new budgets. To change what existing
  ones use, change it in the app, or ask the maintainer first: it's their rule.
- Decided 2026-11-12 (issue #166), with #153's default option.
