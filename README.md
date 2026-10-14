# I Need A Budget

Envelope budgeting (every dollar gets a job, like YNAB or Actual Budget) as a Mac app,
for any bank or credit union. You download your transactions from your bank's website
and the app does the rest: imports them without duplicates, cleans up the payee names,
categorizes what it recognises, and asks you only about the rest.

Everything stays on your Mac. The only thing it ever connects to is GitHub, to update
itself.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/dashboard-dark.png">
  <img alt="The Dashboard: Ready to assign, the Inbox, what's overspent, cash, and charts of net worth and income against spending" src="docs/screenshots/dashboard-light.png">
</picture>

<sub>Screenshots use made-up data (`scripts/demo_data.py`).</sub>

## Install

1. Download the `.dmg` from the [latest release](https://github.com/DigitalN/I-Need-A-Budget/releases/latest).
2. Open it and drag **I Need A Budget** into **Applications**.
3. Open it. The first time, macOS says it can't check the app for malware (it isn't
   notarized by Apple). Click **Done**, then in **System Settings → Privacy & Security**
   scroll down and click **Open Anyway**. That's only needed once.

From then on it updates itself: it looks for a new version each time it opens and
installs it when you quit. **Release notes** in Settings shows what changed.

Building it yourself is in [DEVELOPMENT.md](DEVELOPMENT.md).

The first time it opens, it walks you through four steps:

1. **Files:** import the bank downloads (and any Actual Budget export) in your Downloads
   folder. Overlapping files are fine.
2. **Categories:** a starting set, editable any time.
3. **Balances:** each account's balance, asked once (Quicken files fill it in).
4. **Start month:** start a few months back and catch them up in one click.

## Using it

**Getting transactions in.** Download your transactions from your bank's website: the
app imports them from Downloads on its own, or drop them on the window. **Quicken (QFX),
Money (OFX) or QuickBooks (QBO)** files are best: nearly every bank offers them, they
carry the balance and need no setting up. Tangerine's and RBC's CSVs work as they are;
any other bank's CSV needs its columns shown once. Nothing is imported twice.

**Accounts.** A file from an account the app hasn't seen asks whether it's new or one
you have (a replaced card has a new number). An account imported twice is fixed with
**Merge into…**.

**Dashboard.** Ready to assign, the Inbox, what's overspent and your cash at a glance,
then net worth and income against spending by month, and what each category group
spent (↑ marks a month well above usual). Click any figure for its transactions.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/inbox-dark.png">
  <img alt="The Inbox: a refund, a new payee and an e-transfer, each with its suggested category" src="docs/screenshots/inbox-light.png">
</picture>

**Inbox.** Only what the app isn't sure about, with its best guesses. Click one (or
press 1–4) to categorize it; ⌘Z undoes anything.

- Categorizing one teaches the app, and the rest from that payee follow. **One at a
  time** stops that for a group, **Always ask** for a payee. People you e-transfer
  start with Always ask on.
- Interest, fees and rewards are never asked about.
- Card payments become transfers; refunds go back to the category they came from.
- "SAFEWAY #6432" and "SAVEWAY 1532" both become Safeway.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/budget-dark.png">
  <img alt="The Budget page: Ready to assign and each category's Budgeted, Assigned, Spent and Available" src="docs/screenshots/budget-light.png">
</picture>

**Budget.** **Budgeted** is a category's plan; Assigned is the money really in it.
**Assign money…** shares out a paycheque, topping categories up to their budgeted
amounts in order; **→** and **←** assign one category its budgeted amount or what it
spent. Click a category for its transactions, past spending and notes.

**Ready to assign** is money you really have. **Overspending** in Settings picks how
overspending is paid for: **This month** (out of Ready to assign straight away), **Next
month** (like YNAB) or **Carry over** (the category starts next month below zero).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/accounts-dark.png">
  <img alt="The Accounts page: chequing, a credit card, and paying down what's owed on it" src="docs/screenshots/accounts-light.png">
</picture>

**Credit cards** work like YNAB: card spending from a category sets that money aside in
the card's payment category. Assign extra to it to pay down a balance; Accounts shows
when it'll be paid off.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/subscriptions-dark.png">
  <img alt="The Subscriptions page: subscriptions and bills with their price, next charge and cost a month" src="docs/screenshots/subscriptions-light.png">
</picture>

**Subscriptions and bills** are found automatically, each with a price type:

- **Same Price** (Netflix): a different price asks: a price rise, something new, or a
  one-off?
- **Same Price, Different Currency** (billed in US dollars): exchange-rate wobble is
  ignored.
- **Fluctuates Every Time** (power): any amount is the bill.

Give one an end date or mark it cancelled, and a later charge is flagged in the Inbox.

## Privacy

Your data lives in `~/Library/Application Support/I Need A Budget` (backed up daily, 14
days kept, and before any upgrade) and never in this repository. Bank files and
databases can't be committed: a git hook refuses them, along with card numbers and
anything in your local `.git-denylist`.

## License

[GNU GPL v3](LICENSE): use it, change it and share it. Anything you share that's built
from it must be under the GPL too, with its full source, so it can't become a closed
product. Keep the [NOTICE](NOTICE) file with it so the original project stays credited.
GitHub's **Cite this repository** button (from [CITATION.cff](CITATION.cff)) gives the
reference to use.
