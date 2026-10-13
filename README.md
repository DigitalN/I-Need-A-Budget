# I Need A Budget

Envelope budgeting (every dollar gets a job, like YNAB or Actual Budget) as a Mac app,
for any bank or credit union. You download your transactions from your bank's website
and the app does the rest: imports them without duplicates, cleans up the payee names,
files what it recognises, and asks you only about the rest.

Everything stays on your Mac. The only thing it ever connects to is GitHub, to update
itself.

## Install

Double-click **First Time Setup.command** once. It installs what's needed, switches on
the privacy guard for git, builds the app into Applications and opens it. After that the
app updates itself from GitHub releases, and **Build I Need A Budget.command** rebuilds
from your own copy of the code.

The code is private, so updates need GitHub access on that Mac: the GitHub CLI signed in
(`gh auth login`), or a read-only token pasted in Settings → GitHub access.

The first time it opens, it walks you through four steps:

1. **Files:** it lists the bank downloads (and an Actual Budget export, if you have
   one) in your Downloads folder. Import them. Overlapping files are fine.
2. **Categories:** your envelopes. Edit them any time.
3. **Balances:** what your bank shows for each account right now. Asked once (files
   that include the balance fill it in themselves).
4. **Start month:** start a few months back and catch those months up in one click, so
   your history isn't lost.

## Using it

**Getting transactions in.** On your bank's website, download an account's transactions.
**Quicken (QFX), Money (OFX) or QuickBooks (QBO)** files are best: nearly every bank
offers them, they include the balance, and they need no setting up. The app also reads
Tangerine's and RBC's CSVs as they are, and any other bank's CSV once you've shown it
which column is which (it remembers, so the next file from that bank imports on its
own). Files that land in Downloads are imported automatically and moved into the app's
own folder; a CSV that needs setting up waits at the top of the Inbox. You can also
drop files on the window or use **Import files…**. Nothing is ever imported twice, even
when the same account comes in as a CSV one day and a Quicken file the next.

**Accounts** each have a bank (type any bank or credit union; well-known ones are
suggested). If an account ever ends up in the app twice, it says so and **Merge into…**
puts them back together.

**Inbox.** Only what the app isn't sure about shows up here, with its best guesses.
Click one (or press 1–4, Return for the first) and it's filed. ⌘Z undoes anything.

- A payee's transactions share one card; file them together, or **File one at a time**.
  **Always ask** gives each its own card and stops the app filing that payee on its own.
- Filing one teaches the app: the rest from that payee it's now sure about file
  themselves. Except with **File one at a time**, and except e-transfers with people:
  those are always asked about one by one, never assumed or grouped.
- The finder at the top searches the inbox, and has a button per payee to show just theirs.
- Card payments are matched with the chequing withdrawal and marked as transfers.
  Refunds and e-transfers that pay you back go back to the envelope the money left from.
- "SAFEWAY #6432" and "SAVEWAY 1532" both become Safeway.

**Budget.** Each month: what each category has, spent and got. **Assign money…** shares
out a paycheque, filling categories up to their monthly amount in order. Click a
category to set its monthly amount (or use **Last month** or **Last 3 months avg** of what
it actually spent), see what it needed in past months, move money, and keep notes. It
all saves as you type.

**Credit cards** work like YNAB: money for card purchases moves into the card's payment
envelope, so the payment is covered when it's due. To pay down a balance, assign extra
to the payment envelope. The Accounts page shows what you owe, what's set aside, the
interest you've paid, and when it'll be paid off.

**Subscriptions and bills** are found automatically. Track each one with its own name,
envelope and price type:

- **Same Price** (Apple, Netflix): any other price from that payee is a question: a price
  rise, something new, or a one-off? One payee can have several, told apart by price.
- **Same Price, Different Currency** (services billed in US dollars): small exchange-rate
  changes are ignored; a real price change isn't.
- **Fluctuates Every Time** (power, utilities): any amount from that payee is the bill;
  the page shows the typical amount and range.

Give one an end date, or mark it cancelled, and you're warned in the Inbox if it charges
you after that. Filing a charge under Subscriptions asks "What's this for?" so it can be
tracked by name.

## Privacy

Your data lives in `~/Library/Application Support/I Need A Budget` (backed up daily, 14
days kept, and before any upgrade) and never in this repository. Bank files and
databases can't be committed: a git hook refuses them, along with card numbers and
anything in your local `.git-denylist`.
