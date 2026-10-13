# I Need A Budget

An envelope budget (like YNAB or Actual Budget) built around the files banks let you
download, as a Mac app: Python + pywebview, one window, SQLite. Building, the release checklist, how
updates work and the dev preview are in @DEVELOPMENT.md. What it does for the user is in
README.md.

The maintainer banks with Tangerine: a chequing account and a Mastercard, maybe a line
of credit later. Other people's copies will use other banks (RBC, ATB, credit unions),
so nothing may assume Tangerine. Data comes from the files banks let you download (OFX /
QFX / QBO, CSV). They moved here from Actual Budget and have restarted budgets several
times before: losing history or having to rebuild is the thing to avoid.

## Privacy: the first rule

**No private financial data may ever reach GitHub.** That covers bank files, the
database, account or card digits, people's names (e-transfers), employers, payees
that are local businesses, category names that reveal finances, and real amounts.

- All data lives outside the repository: `~/Library/Application Support/I Need A Budget`
  (database, settings, `Imported Files/`, `Backups/`) and `~/Library/Logs/I Need A Budget`.
- `.gitignore` excludes bank files and databases. `scripts/hooks/pre-commit` (switched on
  with `git config core.hooksPath scripts/hooks`; First Time Setup does it) refuses
  commits with those files, card-number shapes, or any word in `.git-denylist`, a local,
  ignored list of private words. `tests/test_privacy.py` checks the working tree the same
  way. Never bypass the hook, and never weaken these checks.
- Tests and examples use made-up data only ("SAMPLE CAFE CALGARY AB", made-up amounts).
  Build card file names in code (`tests/helpers.CARD_FILE`) so no card-number shape
  appears in the source.
- `payees.BRANDS` is for well-known chains only, never local businesses someone uses.
- The user's own categories were seeded straight into their data folder by a one-off
  script outside the repo. There's no personal seed data in the code.
- To test against real data, use a throwaway folder (`INAB_DATA_DIR=…`, or the preview's
  default `~/Library/Caches/I Need A Budget-preview`) and copies of the files. Don't
  print real data into commit messages, docs or PRs.

## Debugging

Application logs: `~/Library/Logs/I Need A Budget/app.log` (rotating, 3 × 1 MB). Page
(JavaScript) errors are forwarded there too. When the user reports a bug, **read the log
first**. The updater logs `Automatic updates off: <reason>`, `Up to date at`,
`Downloading`, `ready to install`, `Installed …` or `Update check failed: …`. "Up to date"
also when no release has been published yet: merging isn't releasing.
`Page connected` means the window loaded and reached Python.

To reproduce without touching the user's data: `INAB_DATA_DIR=<tmp folder>` (logs go to
`<tmp folder>/Logs/`), and `INAB_TODAY=2026-10-12` to pin "today". To see the interface,
run the dev preview (`python3 scripts/preview.py`, or the `preview` entry in
`.claude/launch.json`) and open it in the browser pane: it's the real page driving the
real engine over localhost. Claude's shell can't screenshot the app's own window.

## Architecture

All in `Application Files/`:

- `main.py`: launcher. Logging, single-instance lock, daily DB backup, the pywebview
  window, the Downloads watcher thread, the updater, and `Api`, which exposes one method,
  `call(name, args)`, to the page. `VERSION` here is the app's only version number.
- `service.py`: everything the page can ask for. `BudgetService.dispatch()` runs only
  methods marked `@exposed`; results are `{"ok": true, "result": …}` or
  `{"ok": false, "error": "…"}`. Raise `UserError` for messages the user should see.
  The preview and most tests drive this same class.
- `db.py`: schema, upgrades, the shared connection behind a lock, **undo** (triggers record
  inverse SQL; `db.write("Label")` makes one undoable step; updates only record changed
  columns), daily backups. Schema is at version 3. To change it: raise `SCHEMA_VERSION`,
  add the column to `SCHEMA` *and* an `ALTER TABLE` step in `_migrate` for existing
  databases. Upgrades copy the database to `Backups/budget-before-upgrade-…` first. The
  user's real database has real data in it: test every upgrade on a copy of it first.
- `importer.py`: detects and parses OFX/QFX/QBO from any bank, Tangerine card and bank
  CSVs, RBC's CSV (every account in one file, so a file has `parts`), CSVs set up once
  (`csvlayout.py`), and Actual Budget's export (learnt from, never imported as
  transactions). Account matching, fingerprint dedup, then `file_transactions()` files
  what it can. Its docstring has the rules.
- `csvlayout.py`: pure. Any bank's CSV: spots a table of dates and amounts (skipping a
  preamble), guesses which column is which, names the layout by its header, reads rows.
- `banks.py`: bank names. Free text on accounts; `canonical()` turns what files call a
  bank into one spelling ("Royal Bank of Canada" → RBC) and `slug()` keys accounts by
  bank and number across file types. `SUGGESTED` only feeds suggestions while typing.
- `payees.py`: pure. Cleans bank descriptions (processor prefixes, store numbers,
  truncated towns/provinces), recognises chains (`BRANDS`), e-transfer people, EFT and
  bill-payment organisations, and hints (transfer / interest / income / cash / fee).
  Tangerine's wording first, then other banks' (RBC, TD, BMO...), which only see text
  Tangerine's rules didn't match. `find_similar()` is the fuzzy match (rapidfuzz):
  "SAVEWAY 1532" → Safeway.
- `ledger.py`: accounts and balances (worked out from one anchor balance), the special
  categories (Income, a payment envelope per debt account, Interest & Fees),
  resolving/renaming/merging payees, spotting and merging an account imported twice.
- `learn.py`: category guesses from history (amount- and recency-weighted votes), mixed
  payees, unusually big charges, refunds, e-transfer shares of a bill, card-category hints.
  `analyze()` returns options, whether it's sure, a reason, and an inbox group key.
- `transfers.py`: pairing the two halves of a transfer between own accounts.
- `subscriptions.py`: detection (steady rhythm; same price, or a once-a-month bill that
  varies), tracking with a name, category, price type and optional end date, price-change
  questions, "charged you again" alerts (after cancelling or after the end date).
- `budget.py`: pure envelope math, credit cards included, and the "overspending carries
  over" setting. Its docstring states the rules and the invariant `tests/test_budget.py`
  checks on random histories (both ways).
- `updater.py`, `launch.py`: updates from the repo's GitHub releases (with a token if
  there is one, else anonymously, for when it's public); moving into Applications;
  relaunching.
- `settings.py`: JSON settings, validated on every load and save.
- `util.py`: money in cents, months and dates.
- `index.html`: the whole interface, vanilla JS, no external resources. Pages: Budget,
  Inbox, Transactions, Accounts, Subscriptions, Settings, plus first-run onboarding.

## Key rules

- **Money is integer cents** everywhere (Python and the page's API). Negative means money
  left the account. Dates are ISO strings, months `YYYY-MM`.
- **Never change `importer.fingerprint()`** (or `fingerprints()`): already-imported rows
  would stop matching and every overlapping file would import twice. Two identical rows in
  one file are both kept (the occurrence count is part of the fingerprint). A row is also
  a duplicate of one from a *different kind* of file in the same account with the same
  date and amount (each matched once): a bank's CSV and Quicken file word things differently.
- **Never change what `payees.describe()` returns for text it already handles**: a
  changed key splits a payee in two. Add other banks' wording after Tangerine's rules,
  and check it on a copy of real data (every existing transaction must describe the same).
- **Accounts are never guessed across banks.** A file finds its account by bank and
  number (`number:<bank>:<last4>`, the same from a CSV or a Quicken file), Tangerine's by
  file name too, else the one account already holding its rows; otherwise it's a new
  account, or for a set-up CSV a question. Tangerine labels every bank account SAVINGS in
  its OFX/QFX files (chequing too): ACCTTYPE isn't trusted for Tangerine.
- **A file's own balance (OFX LEDGERBAL) replaces any balance as old or older**: a
  balance typed in during the day misses what the bank adds later that day.
- **Learning only uses `status = 'done'` transactions** (plus Actual examples). Rows waiting
  in the inbox may carry a tentative category so the budget is roughly right, but they
  never teach the app anything.
- **Subscriptions have a price type** (`subscriptions.pricing`; shown to the user as "Same Price",
  "Same Price, Different Currency" and "Fluctuates Every Time", their wording): `fixed` (exact price ±2¢;
  any other price from that payee is a question: "new price for X, a new subscription, or
  a one-off?", because Apple bills several things under one name), `currency` (billed in
  US$: a wobble within 6% is the same price, not a change; set automatically from the
  card's "Foreign Currency Spend" tag or repeated drift), and `varies` (a power bill: any
  amount from that payee; the page shows the typical amount and range). Detection
  suggests `varies` for a once-a-month payee whose amount moved at least twice.
- **Optional end date** on a subscription: a charge after it (or after it was marked
  cancelled) goes to the inbox with a warning containing "charged you again" (the inbox
  and re-filing keep reasons with that phrase).
- A charge more than 3× the payee's biggest past charge always asks.
- **The updater is the only network access.** No HTTP server in the app (the dev preview
  is a separate script), no analytics, no CDNs or remote fonts. (Settings → Release notes
  opens GitHub in the browser; the app itself doesn't fetch it.) The CSP in `index.html`
  sets `connect-src 'none'`. A future Tangerine sync would be the second network access:
  it must be our own code (see Roadmap).
- pywebview exposes every public attribute of `Api` to JavaScript; keep internals behind a
  leading underscore. AppKit calls run on the main thread (`AppHelper.callAfter`).
- Every user-facing change should be undoable: wrap it in `db.write("Short label")`.
  Background imports aren't undoable (they're removed from Accounts → Imported files).
- `~/Documents` is iCloud-synced on this machine; `scripts/build.sh` builds in
  `~/Library/Caches` and runs `xattr -cr` before signing.

## Budget rules (summary; budget.py has the detail)

Ready to assign starts as the cash in chequing/savings when the budget starts; income
adds, assigning takes. Overspending resets the category and comes out of next month's
Ready to assign (or, with the setting on, stays in the category below zero). Card spending from a funded category moves that money to the card's
payment envelope; uncovered card spending is new debt instead. Paying the card from
chequing spends the payment envelope; old debt is paid down by assigning to it. Refunds
and reimbursements go back into the category they came from. "Needs ~$X" on the Budget
page is the average of what a category actually spent in the last 3/6/12 whole months
(from every month with data for all accounts, before the budget start too).

## Releasing

- A release means the whole **Release checklist** in DEVELOPMENT.md, every time.
- **Every PR raises `VERSION` itself** (in `Application Files/main.py`), so merging is
  all the maintainer does: 1.0.1 → 1.0.2 by default, 1.0.x → 1.1.0 only for a change big
  enough to deserve it. Never leave the bump for a separate PR after the merge.
- Merging to `main` is not a release, but it's the go-ahead for one: Claude then runs
  `scripts/release.sh NOTES.md` (notes outside the repo). The maintainer never builds. Installed copies only update when a release is
  published on GitHub.
- The repository is **private** for now (the maintainer may make it public). It's
  **GPL-3.0-only**, the maintainer's choice, made knowing the GPL allows selling: what
  they want is that nobody builds a closed product from it. NOTICE is a section 7(b)
  attribution term; CITATION.cff gives GitHub's "Cite this repository". The updater uses a token
  saved in the Keychain from Settings, or the GitHub CLI's login (`gh auth token`), and
  asks anonymously without one. Settings shows the token field only when GitHub refused
  this copy (`needs_access`).
- Ask once before anything on GitHub (pushing, opening a PR, publishing a release), then
  do all of it. Check each step's result before the next one that can't be undone.
- Releases are signed with the maintainer's Apple Development certificate (team
  `XT4LT7MS6Y`); installed copies refuse updates from any other team. No Developer ID or
  notarization.
- Releases have only the `.dmg`.

## Testing on the maintainer's Mac

- Their real copy runs from `/Applications` with real data. Don't quit it, reinstall it
  or touch `~/Library/Application Support/I Need A Budget` without asking.
- Say so before opening windows on screen for a test. For the built app, launch a copy
  against a throwaway folder: `open -n --env INAB_DATA_DIR=/tmp/x "/Applications/I Need A Budget.app"`
  (and point `downloads_dir` in that folder's `settings.json` somewhere harmless, or the
  watcher will import from the real Downloads).
- After testing, quit test copies and delete their folders.
- To try the interface on real data: copy the database with SQLite's backup API (read-only
  source) into `~/Library/Caches/I Need A Budget-preview/`, write a `settings.json` there
  whose `downloads_dir` is an empty folder, run the `preview` launch entry, and delete the
  folder afterwards. The preview loads `service.py` once: restart it after Python changes.

## Git

- Work on a branch, not `main`. The maintainer opens pull requests with Create PR.
- The pre-commit hook must stay on (`git config core.hooksPath` → `scripts/hooks`).

## Decisions already made

Don't undo these without asking:

- Data in by **auto-import from Downloads**: recognised bank files are imported when
  they appear (or at launch) and moved into `Imported Files/`. Dropping files on the
  window also works (there's no Import files button: the maintainer removed it).
  Actual exports are only learnt from, never moved.
- **A bank file for an account the app hasn't seen asks** "a new account, or one you
  have?" (a replaced card has a new number) once the budget is set up and there's an
  open account of the same sort (card or bank) it could be. Dropped files ask straight
  away; files from Downloads wait at the top of the Inbox ("Don't import" ignores them).
  The answer is remembered by the file's account number. During onboarding every
  account is simply created.
- **Any bank, not just Tangerine.** Quicken/Money/QuickBooks files are suggested
  wherever the app explains downloads (they need no setup and carry the balance). Other
  CSVs are **set up once** in a pop-up (columns, date order, money out sign, account);
  the layout is remembered by header and the account by file name with dates removed, so
  the next file imports on its own and a new name only asks which account. Until then
  the file waits at the top of the Inbox ("Not a bank file" ignores it for good). A CSV
  you chose or dropped opens the pop-up straight away; one the watcher found shows a toast.
- **The bank on an account is free text** with suggestions as you type (`banks.py`),
  never a fixed list: there are too many credit unions. New accounts are named "Bank +
  type" (editable, and Edit… changes name and bank any time); existing ones keep theirs.
- **An account imported twice** is flagged (Inbox and Accounts) and fixed with Merge
  into…: copies dropped (keeping your filing, notes, splits, transfers, bank IDs), the
  rest moved, undoable.
- **Review inbox** instead of a register: only what the app isn't sure about, with one
  click / keypress per decision, same-payee items and bill shares grouped into one card.
  Filing one teaches the app and re-files the rest of that payee, **except**:
  - **"File one at a time"** on a group means exactly that: filing one never files the
    others (`approve(..., refile=False)`).
  - **Transfers with people are never assumed**: e-transfers to or from a person are
    never filed automatically, never grouped by person, never pre-filled with a guess,
    and never treated as refunds. The last choice is only suggested. (The user hit this
    with several transfers to one person, each for something different.)
- **Filing a charge into a subscriptions category** (or one that charges on a steady
  rhythm) asks "What's this for?": name it to track it as a subscription or bill, or
  "Not recurring" to stop asking about that payee at that price.
- The inbox shows today's reasoning (re-analysed on each load), and pending items are
  re-filed under the current rules at every launch.
- **Inbox grouping is controlled by the payee's "Always ask" switch**: off, a payee's
  items share one card (even a mixed payee like Amazon, split with "File one at a time");
  on, one card each and never filed without asking. People, refunds and unusually big
  charges always get their own card. `learn.analyze()` sets the group key.
- The inbox has a finder (search + a button per payee with counts) so nothing gets lost
  in a long list.
- **Payee clean-up with fuzzy matching** (store numbers, typos, truncation).
- **Monthly budget per category** ("each month"), with "needs ~$X" from *actual* spending,
  so it's right even when the user forgot to budget a month. "Each month" is a plan, not
  money: Assign money… tops a category up to it, counting what's already in it (carried
  in plus assigned this month). The maintainer once read "Still needs" as being asked
  to assign twice; the window now shows Each month / Already in / Assign. The category panel offers
  "Last month" and "Last 3 months avg" (both actual spending), saves the amount as it's
  typed (no Save button), and has a per-category Notes box that also saves as typed.
- **The category pop-over** (click a category's name or its Spent figure) is wide and in
  the maintainer's order: this month's transactions (adding up to Spent: a split counts
  only its line; a card's payment envelope lists payments and the card spending moved in,
  from `budget.compute`'s `moved_from`), what it actually needed, Move money, Notes, Each
  month. A group's Spent opens the same list for the whole group. Clicking a transaction
  opens it (it has a Category field) and the pop-over reopens with the change.
- Budgeting is **as each paycheque lands** (Assign money… fills categories up to their
  monthly amount, in order), not a lump at the start of the month.
- **Credit cards YNAB-style** (payment envelopes, paying down a balance), with room for a
  line of credit as a third, debt account.
- **Refunds and reimbursements go back to the category the money left from.**
- **Subscriptions page** that treats subscriptions as their own thing: named per price,
  own category, price changes, late/cancelled tracking, "charged again" alerts. New prices
  from a subscription payee always ask (see Key rules). A charge opened on the
  Transactions page has a Subscription line: Track as subscription… or Edit….
- **Window only, no menu bar item.** The close button asks, then quits the app entirely;
  ⌘Q quits without asking. A downloaded update installs on quit.
- "Eating Out" was renamed **Food** (Actual's old name still matches through `old_names`).
- **Update checks only at launch and on Check now**, never on a timer while the app is
  open (the maintainer's choice).
- **Auto-updates via GitHub releases.** The user may later give a separate copy to their
  spouse or a friend (separate data, same updates). While the repo is private that copy
  needs a read-only token in Settings; once it's public, nothing. People install from
  the release's `.dmg` (README), never by building.
- Budget can start months back; past months are caught up with one click ("Catch up").
- **Overspending carries over** is a setting, off by default (a friend coming from YNAB
  asked for it): an overspent category starts next month below zero instead of the
  amount coming out of Ready to assign. Only cash overspending; card overspending is
  debt on the card either way.
- **Settings is kept short** (the maintainer's mock-up): Appearance, Import from
  Downloads, Overspending carries over, Learn from Actual; updates, Release notes,
  Check now (GitHub access only when needed); Open buttons for the data, imported
  files and log folders. Budget start, the "needs" look-back and "fill monthly amounts"
  were removed from it on purpose.

## Roadmap (future chats)

- **Sync with Tangerine** (deferred by the user): a window showing tangerine.ca where
  the user signs in themselves (PIN, 2FA), then the app reads transactions through that
  session. It must be **entirely our own code**: no third-party Tangerine libraries (the
  user's explicit requirement), no stored password. The CSV/OFX import stays as fallback.
- "Fund bills" one-click, bill due dates, safe-to-spend until payday.
- A line of credit account: any bank's CSV can be set up now. Balance transfers between
  debt accounts already work (no envelope change).
- Spending reports (month over month, by payee).
- A **Dashboard** home page (the user asked where it was; offered, not yet decided):
  ready to assign, balances and what's owed, inbox count, overspent or low envelopes,
  bills due before the next paycheque, spending against what each category needs.

## Known gaps

- Real Tangerine QFX/OFX files have been tried (that's how the SAVINGS label was found).
  RBC's CSV, other banks' OFX and the payee wording for RBC/TD/BMO e-transfers, payroll
  and bill payments are built from public descriptions and made-up files: check them
  against a real file when one turns up.
- Updating works: 1.0.0 → 1.0.1 installed through the updater on 2026-10-13. The 404
  handling changed in 1.0.2 (no release vs no access); it's unit-tested, not yet live.

## Interface rules

- Every pop-up has a ✕ top right that closes without saving (the `dialog()` helper adds
  it). Save with no changes just closes, with no "Saved" message.
- A popover opened from a pop-up (the category list) is attached inside the open
  `<dialog>`: dialogs live in the browser's top layer, above anything else on the page.
- Every clickable thing needs a handler: `[data-act]` buttons are dispatched by the
  document click handler (plus each page's action function), suggestion chips by
  `[data-pick]`. `tests/test_page.py` fails on a button action nobody handles. A button
  that silently does nothing is the worst kind of bug here: it happened once.
- Amounts and notes in side panels save as they're typed (debounced), no Save button.
- Copy is short and plain. The user trims explanations that don't earn their place;
  don't add paragraphs of help text to forms.
- **Test the interface with the mouse**, not only the keyboard, in the dev preview on a
  copy of the real data (see Testing). Several bugs only showed up when clicking.

## Working style

- When fixing a reported problem, think through the related cases and fix the whole
  class, so the maintainer doesn't have to report each one.
- The maintainer often adds requests mid-task: fold them into the work in progress.

## Tests

`python3 -m pytest` (tests/, all synthetic data). `tests/test_privacy.py` fails if the
working tree has anything from `.git-denylist` or a card-number shape.
