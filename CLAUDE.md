# I Need A Budget

An envelope budget (like YNAB or Actual Budget) built around the files banks let you
download, as a Mac app: Python + pywebview, one window, SQLite. Building, the release checklist, how
updates work and the dev preview are in @DEVELOPMENT.md. What it does for the user is in
README.md. The words for things (categorize, file, transfer vs e-transfer, payment
category, recurring charge...) are in `CONTEXT.md`: use them in the page, docs, comments
and issues.

The maintainer banks with Tangerine: a chequing account and a Mastercard, maybe a line
of credit later. Other people's copies will use other banks (RBC, ATB, credit unions),
so nothing may assume Tangerine. Data comes from the files banks let you download (OFX /
QFX / QBO, CSV). They moved here from Actual Budget and have restarted budgets several
times before: losing history or having to rebuild is the thing to avoid.

## Privacy: the first rule

**No private financial data may ever reach GitHub.** The repository is public: code,
commits, issues, PRs and release notes can all be read by anyone. That covers bank files, the
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
  columns), daily backups. Schema is at version 6. To change it: raise `SCHEMA_VERSION`,
  add the column to `SCHEMA` *and* an `ALTER TABLE` step in `_migrate` for existing
  databases. Upgrades copy the database to `Backups/budget-before-upgrade-…` first. The
  user's real database has real data in it: test every upgrade on a copy of it first.
- `importer.py`: detects and parses OFX/QFX/QBO from any bank, Tangerine card and bank
  CSVs, RBC's CSV (every account in one file, so a file has `parts`), CSVs set up once
  (`csvlayout.py`), and Actual Budget's export (learnt from, never imported as
  transactions). Account matching and fingerprint dedup; it only adds rows, whose ids then
  arrive in the inbox (`Inbox.arrive`). Its docstring has the rules.
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
  categories (Income, a payment category per debt account, Interest & Fees),
  resolving/renaming/merging payees, spotting and merging an account imported twice.
- `learn.py`: category guesses from history (amount- and recency-weighted votes), mixed
  payees, unusually big charges, refunds, e-transfer shares of a shared expense,
  card-category hints.
  `analyze()` returns options, whether it's sure, a reason, and an inbox group key.
- `inbox.py`: an inbox item from arrival until it's categorized, split or a transfer.
  `Inbox(conn, today)`: `arrive` (an import's new rows), `recheck`, `view` (what the Inbox
  page shows: suggestions, today's reason, the item's question, its final group key and
  guess), and every decision: `categorize(txn_ids, category_id, recheck_payee)`,
  `accept_all`, `split`, `mark_transfer`, `not_a_transfer`, `send_back`, `ask_again`,
  `same_payee`, `new_price`, `set_always_ask`. **Only it puts a transaction in the inbox or
  takes it out**, from every page (the Transactions page and the transaction pop-up too).
  It asks learn.py for suggestions and subscriptions.py for the recurring-charge rules;
  service.py wraps each call in its undo step. `tests/test_inbox.py` drives it directly.
- `transfers.py`: pairing the two halves of a transfer between own accounts, for the inbox.
- `subscriptions.py`: detection (steady rhythm; same price, or a once-a-month bill that
  varies), tracking with a name, category, price type and optional end date, price-change
  questions, "charged you again" alerts (after cancelling or after the end date). It links
  charges and keeps prices; what happens to a charge in the inbox is inbox.py's.
- `budget.py`: pure envelope math, credit cards included, and the three ways overspending
  is paid for (the Overspending setting). Its docstring states the rules and the
  invariant `tests/test_budget.py` checks on random histories (all three ways).
- `figures.py`: every money figure the pages show (Budget page, Dashboard, category
  pop-over and Spent list, Debt page, Assign money…, Catch up), from one reading of the
  database per request, with `budget.compute` as its core. It only reads: `service.py`
  makes the changes and arranges the figures for the page. Working one of these figures
  out anywhere else (service.py, index.html) is a bug. `tests/test_figures.py` checks
  it on random databases.
- `updater.py`, `launch.py`: updates from the repo's GitHub releases (anonymously, or
  with a token if there is one); moving into Applications;
  relaunching.
- `settings.py`: JSON settings, validated on every load and save.
- `util.py`: money in cents, months and dates.
- `index.html`: the whole interface, vanilla JS, no external resources. Pages: Dashboard
  (where it opens), Budget, Inbox, Transactions, Accounts, Subscriptions, Settings, plus
  first-run onboarding.

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
  in the inbox never teach the app anything, and count in no category: no tentative
  category, Spent/Usual/the budget only count `status = 'done'`
  ([ADR 0002](docs/adr/0002-inbox-items-count-in-no-category.md)). The inbox's "Accept
  all" uses each item's `guess` (`Inbox.view`, from `learn.guess()`: never an e-transfer,
  a question or one you sent back).
- **What you decided stays decided** ([ADR 0005](docs/adr/0005-what-you-decided-stays-decided.md)).
  Who decided is a fact: `transactions.by_you`. Yours: what you categorize (one, a group,
  Accept all), split, mark as a transfer or Not a transfer, send back, or add by hand.
  The app's: what an import or the re-check categorizes, and what removing an import (or
  a merge) leaves to decide again (`Inbox.ask_again`). Nothing the app does on its own
  changes a transaction that's yours: the re-check skips it, and `find_counterpart` never
  pairs with it (a half arriving for one you categorized waits in the inbox: "Looks like
  the other half of…"; Transfer pairs them). Not a transfer sends both halves back to
  wait, never paired again.
- **`reason` is only text to show; no code reads it back.** "Charged you again",
  "Matched to X (similar name)" and "You sent this back" are worked out each time
  (`subscriptions.charged_when_it_shouldnt`, `payee_keys.how = 'similar'`, `by_you`).
- **A recurring charge's price follows a charge only when it first arrives** or when you
  choose "New price for…"; the re-check never changes a price. Its category likewise:
  moving a recurring charge to another category only sends the charges that arrive from
  then on there; its past charges stay where they were (issue #29).
- **Subscriptions have a price type** (`subscriptions.pricing`; shown to the user as "Same Price",
  "Same Price, Different Currency" and "Fluctuates Every Time", their wording): `fixed` (exact price ±2¢;
  any other price from that payee is a question: "new price for X, a new subscription, or
  a one-off?", because Apple bills several things under one name), `currency` (billed in
  US$: a wobble within 6% is the same price, not a change; set automatically from the
  card's "Foreign Currency Spend" tag or repeated drift), and `varies` (a power bill: any
  amount from that payee; the page shows the typical amount and range). Detection
  suggests `varies` for a once-a-month payee whose amount moved at least twice.
- **Optional end date** on a subscription: a charge after it (or after it was marked
  cancelled) goes to the inbox with a "charged you again" warning, worked out each time:
  reactivating it or moving the end date clears it, and its waiting charges are re-checked.
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
adds, assigning takes. Overspending is paid for the way the **Overspending** setting says:
**This month** (the default) takes it out of Ready to assign in the month it happens, so
Ready to assign is always money really there (the maintainer saw Ready to assign at about
twice what was in the bank under YNAB's rule and asked for this); **Next month** is
YNAB's and Actual's rule; **Carry over** leaves it in the category below zero next month.
The first two reset the category next month. Ready to assign + Σ positive Available −
overspending not taken yet + money in waiting in the inbox == cash, and the Budget page
shows that line under it ("= $X in Chequing − $Y in categories − $Z waiting in the
Inbox", flagged if it's ever off). Money out waiting in the inbox has already come off
Ready to assign; money in waits until it's categorized (it may be a reimbursement). With This month, covering
overspending costs nothing (Assign money… counts it as `covered`) and covering it from
another category gives Ready to assign that much back. Card spending from a funded category moves that money to the card's
payment category; uncovered card spending is new debt instead. Paying the card from
chequing spends the payment category; old debt is paid down by assigning to it. Refunds
and reimbursements go back into the category they came from. **Usual** ("usually ~$X"
on the Budget page; it was "Needs", which the maintainer read as the budgeted amount) is
the average of what a category actually spent in the 3/6/12 whole months before a month
(`budget.usual_spending`: from every month with data for all accounts, before the budget
start too, and from the category's own first spending, so a new bill isn't averaged
with months it didn't exist).

## Releasing

- A release means the whole **Release checklist** in DEVELOPMENT.md, every time.
- **Every PR raises `VERSION` itself** (in `Application Files/main.py`), so merging is
  all the maintainer does: 1.0.1 → 1.0.2 by default, 1.0.x → 1.1.0 only for a change big
  enough to deserve it. Never leave the bump for a separate PR after the merge.
- Merging to `main` is not a release, but it's the go-ahead for one: Claude then runs
  `scripts/release.sh NOTES.md` (notes outside the repo). The maintainer never builds. Installed copies only update when a release is
  published on GitHub.
- The repository is **public** (it was private at first). It's
  **GPL-3.0-only**, the maintainer's choice, made knowing the GPL allows selling: what
  they want is that nobody builds a closed product from it. NOTICE is a section 7(b)
  attribution term; CITATION.cff gives GitHub's "Cite this repository". The updater asks
  GitHub anonymously, or with a token when there is one (saved in the Keychain from
  Settings, or the GitHub CLI's login, `gh auth token`). Settings shows the token field
  only when GitHub refused this copy (`needs_access`): it would if the repository went
  private again.
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
  The answer is remembered by the file's account number. Its drop-down offers every
  open account, the file's sort first: what a file says it is can be wrong (Tangerine's
  card QFX once read as chequing and the card wasn't offered). During onboarding every
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
  into…: copies dropped (keeping your categories, notes, splits, transfers, bank IDs), the
  rest moved, undoable.
- **A used category or account can only be hidden or closed**, never deleted (deleting
  one sent its transactions back to the inbox and took its assigned money with it). No
  Merge into… for categories either (the maintainer's call, issue #29). Deleting a
  category you made (Delete… in its pop-over; ✕ in onboarding's list) always asks first:
  one never used ("Delete Gifts? It's never been used, so nothing else changes.") is
  deleted; a used one says what it's used by (`ledger.category_uses()`) and offers Hide
  (`deleteQuestion` in the page). Income and Interest & Fees can only be hidden, card
  payment categories neither. An empty group's ✕ asks too. An account's ✕ is only for
  one never used
  (`ledger.used_category_ids()`, `used_account_ids()`). A closed account keeps its history,
  stays in Net worth and in Cash while it has money, and isn't offered for imports,
  transfers or new transactions; Close account / Reopen account are in Edit…, and closed
  accounts are listed under Closed accounts on the Accounts page. A file that finds a
  closed account (by its number or file name) still imports into it, and it stays closed
  (the maintainer's choice).
- **Review inbox** instead of a register: only what the app isn't sure about, with one
  click / keypress per decision, same-payee items and shares of one shared expense
  grouped together. Categorizing one teaches the app and re-checks the rest of that
  payee, **except**:
  - **"One at a time"** on a group means exactly that: categorizing one never
    categorizes the others (`categorize(..., recheck_payee=False)`).
  - **Outside the inbox** (the Transactions page, the transaction pop-up, the
    "Categorized automatically" list) a category change changes only that transaction:
    no payee re-check (the maintainer's call: a mixed payee like a warehouse store, food
    one week and household the next, must stay as they put it).
  - **A payee is just a name; e-transfers start with Always ask on**
    ([ADR 0001](docs/adr/0001-payee-is-just-a-name.md)). Anyone you e-transfer with
    gets Always ask when first seen, so their e-transfers are never categorized without
    asking and never grouped; the last choice is only suggested. (The user hit this with
    several e-transfers to one person, each for something different.) Switched off, the
    payee is learnt like any other and found as a recurring charge (rent e-transferred to
    a landlord). Whatever the switch, an e-transfer (`hint = 'etransfer'`, on the
    transaction) is never in the inbox's "Accept all", and money in is matched to shares
    of a shared expense, never to refunds.
  - **Bank entries** (interest, fees, rewards: `payees.BANK_ENTRY_HINTS`) are never
    asked about and have no Always ask: each kind (its payee) goes where the newest one
    went, at first Interest & Fees or Income. Cash withdrawals and deposits aren't bank
    entries: they're learnt like any payee.
- **Categorizing a charge into a subscriptions category** (or one that charges on a
  steady rhythm) asks "What's this for?", from every page: name it to track it as a
  subscription or bill, or "Not recurring" to stop asking about that payee at that price.
- The inbox shows today's reasoning (worked out on each load), and **the re-check** runs
  at every launch, after imports and after you categorize: it categorizes the waiting
  items the app is now sure of, never yours.
- **A transaction you sent back** (cleared category, unsplit, Not a transfer) is its own
  inbox item, outside its payee's group and outside Accept all, still with suggestions,
  saying "You sent this back". "Categorized automatically" lists only the app's.
- **Inbox grouping is controlled by the payee's "Always ask" switch**: off, a payee's
  items are one group (even a mixed payee like Amazon, split with "One at a time");
  on, each is its own inbox item and never categorized without asking. Refunds and
  unusually big charges are always their own item, and so are questions (same payee?
  new price?), "charged you again" and anything yours. `learn.analyze()` suggests the
  group key; `Inbox.view()` gives the final one and the guess, and the page has no
  exceptions of its own.
- The inbox has a finder (search + a button per payee with counts) so nothing gets lost
  in a long list.
- **Payee clean-up with fuzzy matching** (store numbers, typos, truncation).
- **A budgeted amount per category** (`monthly_budget`), shown as **Budgeted**: the
  maintainer's word for it (never "Each month", which they disliked). Usual ("Needs ~$X") from
  *actual* spending shows when none is set, so it's right even when the user forgot to
  budget a month. Budgeted is a plan, not money: Assign money… tops a category up to it,
  counting what's already in it (carried in plus assigned this month). The maintainer
  once read "Still needs" as being asked to assign twice; the window shows Budgeted /
  Already in / Assign. Beside each Assigned box, **→** assigns exactly the budgeted
  amount and **←** exactly what it spent this month, undoably; each shows only when it
  would change something (→ not for a $0 budget).
- **Assigned boxes:** empty when nothing's assigned (0.00 is only the placeholder: the
  maintainer kept typing into the middle of "0.00"); clicking into any amount box selects
  it so typing replaces it, also the box Return or Tab moved you to; another click in a box
  you've clicked into places the cursor. It's done on mouse-down, before WebKit places
  the cursor: cancelling the mouse-up (1.0.7–1.2.2) missed a box you were already in, and
  Chromium (the preview) hid it. Test clicks in WebKit, the app's engine. A box
  saves when you leave it (Return, Tab, a click elsewhere), never when a redraw replaces
  it mid-typing. Redrawing the Budget page keeps the scroll position and the box you're
  in (it used to jump to the top after every save: `scrollIntoView` on the month strip
  scrolled the page too). **Math in amount boxes** (issue #28, `parseMoney`, no `eval`):
  any amount box takes a sum (`120+35.50`, `3*45`, brackets), shown as its amount once you
  leave it. In Assigned, Budgeted and Assign money…'s Assign column (`data-relative`) a
  leading `+ − * /` works on what the box had when you clicked in: Food at $120, `+50` →
  $170, `-20` → $100 (so `-50` in an empty box is still −$50). Anywhere else a leading
  minus is a negative amount. Escape in an Assigned box puts it back and saves nothing.
- **A card's payment category is money set aside, not paid**: it stays in the bank until
  a payment, so what you owe doesn't change. Its pop-over shows You owe / Set aside / Not
  set aside yet, its lines say "Set aside for card spending in Food" and "Paid to the card
  from Chequing", and its total says "This month", never "Spent" (the maintainer read
  "Spent" as money already paid to the card).
- **The category pop-over** (click a category's name or its Spent figure) is wide and in
  the maintainer's order: this month's transactions (adding up to Spent: a split counts
  only its line; a card's payment category lists payments and the card spending moved in,
  from `budget.compute`'s `moved_from`), **Monthly Budgeted Amount**, **Subscriptions**
  and **Bills** (each only when it has any), Move money, Notes. Monthly Budgeted Amount is
  one section: the bars of what it actually spent, Last month, then Last 3/6/12 months and
  Recurring charges (each with Use), and the Budgeted field, saved as it's typed (no Save
  button); Notes also save as typed. Recurring charges is what the Subscriptions and Bills
  cards come to a month (`subscriptions.for_category`: the active recurring charges whose
  category this is, where their next charge goes, whether or not one has landed there
  yet). Clicking one opens its Edit… pop-up. The name at the top is a box: click it to
  rename the category (not a card's payment category, named after its card). At the
  bottom, **Hide** (**Show** for a hidden one) and **Delete…**. A group's Spent opens the
  same list for the whole group. Clicking a transaction opens it (it has a Category
  field) and the pop-over reopens with the change.
- **Categories are edited on the Budget page itself** (issue #29; there's no Edit
  categories… pop-up, the maintainer had it removed). A row's tools appear at once while
  the pointer is on that row and fade over 0.5 s when it leaves (a 60 px reach around the
  pointer was tried and was too busy): ⋮⋮ in the card's left margin (part of the row, so
  the pointer reaches it without leaving), and right of the name **+**, **✎** (a pencil, not the
  word "Rename") and, on an empty group, **✕**, which asks. + on a group makes a group
  above it, named in place, with a "New Spending Category" (2, 3… if taken) whose name
  you're then typing, selected: one undo step (`add_group(first_category=True)`). + on a
  category makes a category above it, named in place. ✎ renames in place. In place:
  Return or clicking away saves, Escape or an empty box cancels, a refused name keeps
  you in the box after Return. Drag by ⋮⋮: a category within its group or into another, a
  group among the others; saved on release, no Done. Pressing a group's ⋮⋮ folds every
  group's categories at once, before the mouse moves (waiting for it was hard to use), in 0.1 s (each group is its own `<tbody>`; the cells are wrapped
  so rows can fold), the held group staying under the pointer, so a long list fits. The
  page never scrolls while it folds (it did, and the top of the page slid into view, which
  the maintainer found unintuitive): the card keeps its height and the other groups close
  up around the held one. On release they unfold in 0.1 s too, the group staying where it
  was dropped; the page scrolls only if the group ends up out of sight; it flashes;
  groups you folded stay folded (`beginDrag`, also onboarding's list). A folded row has no
  height at all, borders included, and only the held group's heading is coloured: in
  WebKit each folded row kept its 1px line, leaving gaps (1.2.4). No text is selected
  while the mouse is down (`selectstart`; WebKit selected it despite preventDefault). Debt Payments (the
  cards' group) has ⋮⋮ and + too and moves like any group; Income never moves. A move that
  can't be saved goes back, and the list still unfolds (it once stayed folded). Nothing
  moves into or out of Income or the cards' group, not even within Income: the first
  category there is the app's own Income (`move_category`); the Income group isn't on
  the Budget page at all, so + on the last group is how a group goes at the bottom. **Show
  hidden (N)** under the table lists hidden categories, faded, until the page is left.
  No two categories share a name, nor two groups, in any capitalization; typing a hidden
  category's name offers "Show it again?", which brings it back into that group
  (`show_category`). An import's redraw waits while you drag or type a name.
- **Onboarding's categories step** keeps a list of its own (there's no Budget page yet):
  handles, ✕ that asks in a strip under the row, Add a category…, Add a group… (a group
  gets a "New Spending Category" there too). Its handlers are bound to an element made
  each time it's drawn: bound to the shared pop-up the editor was once in, they piled up,
  one more set per opening, so one Return added a category five times, and Return
  stopped working in other pop-ups.
- Budgeting is **as each paycheque lands** (Assign money… fills categories up to their
  budgeted amount, in order), not a lump at the start of the month.
- **Credit cards YNAB-style** (payment categories, paying down a balance), with room for a
  line of credit as a third, debt account.
- **Refunds and reimbursements go back to the category the money left from.**
- **Subscriptions page** that treats recurring charges (subscriptions and bills) as their
  own thing: named per price,
  own category, price changes, late/cancelled tracking, "charged again" alerts. New prices
  from a subscription payee always ask (see Key rules). A charge opened on the
  Transactions page has a Subscription line: Track as subscription… or Edit….
- **Window only, no menu bar item.** The close button asks, then quits the app entirely;
  ⌘Q quits without asking. A downloaded update installs on quit.
- "Eating Out" was renamed **Food** (Actual's old name still matches through `old_names`).
- **Update checks only at launch and on Check now**, never on a timer while the app is
  open (the maintainer's choice).
- **Auto-updates via GitHub releases.** The user may later give a separate copy to their
  spouse or a friend (separate data, same updates). The repo is public, so that copy
  needs no token. People install from
  the release's `.dmg` (README), never by building.
- **The window** opens at 85% of the screen the first time on a Mac, then the size and
  place it was left: AppKit's frame autosave (`main.WINDOW_FRAME_NAME`, kept in the app's
  macOS defaults, saved on every change), moved and shrunk onto a screen if it no longer
  fits one. The log says `Window <w>×<h>` at launch.
- Budget can start months back; past months are caught up with one click ("Catch up").
- **Overspending** is a setting with three choices (the maintainer wanted to choose how
  the rollover works): This month (default), Next month (YNAB), Carry over (a friend
  coming from YNAB asked for it; it was an on/off switch in 1.0.3–1.0.4, and `settings.py`
  turns that switch into Carry over). Only cash overspending; card overspending is debt
  on the card whichever is chosen.
- **Settings is kept short** (the maintainer's mock-up): Appearance, Import from
  Downloads, Overspending, Learn from Actual; updates, Release notes,
  Check now (GitHub access only when needed); Open buttons for the data, imported
  files and log folders. Budget start, the Usual ("needs") look-back and "fill monthly amounts"
  were removed from it on purpose.
- **No bill due dates, paydays or safe-to-spend**, ever: that isn't the envelope method
  (the maintainer's call). Money is in a category or it's Ready to assign.
- **The Dashboard** is the page the app opens on (always, after onboarding too), at the
  top of the sidebar. It absorbed "spending reports". Four figures, each opening its page:
  Ready to assign (with the check line), Inbox (count and money in waiting), Overspent in
  this month, Cash and what's owed. The months choice (3 / 6 / 12 / All, last whole
  months + this month "so far") sits above everything, scopes the whole page and is
  remembered (setting `dashboard_months`, not shown on the Settings page; the maintainer
  disliked it resetting). Two charts, drawn as SVG in the page (no library; colours
  `--series-1`/`--series-2`, checked for colour blindness on the card in both themes):
  **Net worth** (a bar per month-end: cash above zero, owed below, the net worth line;
  YNAB's style, the maintainer's pick over a daily line; this month's figure in the
  header) and **In and out** (Income and Spending bars per month, the table's figures).
  Both show the months chosen; a month before every account has data is faded with "Not
  every account has data" in its tooltip (Net worth once started at that month, so an
  account added recently made every months choice look the same). Then **Spending**: Income, total
  spending, and the category groups in Budget page order (closed every time the page
  opens; hidden categories only if they spent), months as columns. Figures are Spent's. A whole month at least **$25 and 25%**
  above the category's Usual for it is marked "above usual" (↑), never "overspent" (that's
  below zero); a closed group shows it on its total. Not marked: this month, months
  without data for every account, income. Clicking a figure opens its transactions. No
  by-payee report (the maintainer dropped it: e-transfer payees mix unrelated things).

## Roadmap (future chats)

- **Sync with Tangerine** (deferred by the user): a window showing tangerine.ca where
  the user signs in themselves (PIN, 2FA), then the app reads transactions through that
  session. It must be **entirely our own code**: no third-party Tangerine libraries (the
  user's explicit requirement), no stored password. The CSV/OFX import stays as fallback.
- A line of credit account: any bank's CSV can be set up now. Balance transfers between
  debt accounts already work (no category changes).

## Known gaps

- Real Tangerine QFX/OFX files have been tried (that's how the SAVINGS label was found,
  and that its Mastercard's QFX is a bank statement: `CREDITLINE` with the full card
  number, so a credit line whose number passes the Luhn check is a card).
  RBC's CSV, other banks' OFX and the payee wording for RBC/TD/BMO e-transfers, payroll
  and bill payments are built from public descriptions and made-up files: check them
  against a real file when one turns up.
- Updating works: 1.0.0 → 1.0.1 installed through the updater on 2026-10-13. The 404
  handling changed in 1.0.2 (no release vs no access); it's unit-tested, not yet live.

## Interface rules

- Every pop-up has a ✕ top right that closes without saving (the `dialog()` helper adds
  it). Save with no changes just closes, with no "Saved" message.
- A key the page acts on (Escape closing a pop-over, a shortcut) calls `preventDefault`:
  otherwise the app's window passes it on, nothing takes it, and macOS plays its warning
  sound (it did for Escape on the category pop-over). `tests/test_page.py` checks Escape.
- A popover opened from a pop-up (the category list) is attached inside the open
  `<dialog>`: dialogs live in the browser's top layer, above anything else on the page.
  So is a message (toast) shown while a pop-up is open, or it's hidden under it. There's
  only one pop-up, so a question asked from inside one is a strip in it, not a second
  pop-up.
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

## Agent skills

### Issue tracker

GitHub Issues in `DigitalN/I-Need-A-Budget`, through `gh`. The repo is public, so issues
follow the privacy rule above. See `docs/agents/issue-tracker.md`.

### Triage labels

The five defaults: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`,
`wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` (the glossary) and `docs/adr/` (decisions) at the repo root.
See `docs/agents/domain.md`.
