# I Need A Budget

An envelope budget kept from the files banks let you download. Every dollar of cash is
given a job in a category; the app puts the bank's transactions into categories and asks
only when it isn't sure.

## Accounts and files

**Account**:
A real account at a bank, or a wallet, whose transactions the budget follows. Every
account is either a cash account or a debt account.

**Bank**:
The bank or credit union an account is at, named however the person calls it.
_Avoid_: institution

**Cash account**:
A chequing, savings or wallet account: money that's yours to spend.
_Avoid_: bank account

**Wallet**:
A cash account for notes and coins.
_Avoid_: Cash (as an account type)

**Overdraft**:
A cash account's safety net: the bank lets the balance go below zero. Below zero is
simply less cash; the account doesn't become a debt account.
_Avoid_: line of credit (for this)

**Debt account**:
A credit card or a line of credit: money owed, paid down from a cash account.
_Avoid_: credit account, liability

**Card**:
A credit card. A debit card isn't an account: its charges belong to the chequing account.
_Avoid_: card (for an inbox item)

**File**:
A file downloaded from a bank and imported: OFX, QFX, QBO or CSV. One file can hold
several accounts.
_Avoid_: statement, export, download

**Import**:
Bringing one file's transactions into the budget. An import can be removed again,
taking its transactions with it.

**Actual export**:
A file exported from Actual Budget. The app learns categories from it but never imports
its transactions.

**Duplicate account**:
One real account that ended up as two accounts in the app. It's fixed by merging one
into the other.

**Closed account**:
An account you no longer use, kept with all its history. It isn't offered for imports or
new transactions.
_Avoid_: archived, deleted

## Transactions and payees

**Transaction**:
One line of an account's history: a date, an amount and what the bank wrote.

**Bank description**:
Exactly what the bank wrote for a transaction.
_Avoid_: raw name, memo

**Payee**:
Who a transaction was with, as one name: cleaned up from the bank description so one
store is one payee however the bank spells it. A friend, a store and a landlord are all
just payees.
_Avoid_: vendor, merchant, person

**Mixed payee**:
A payee whose transactions have gone to two or more categories.

**Bank entry**:
A transaction the bank makes on your account itself: interest, a fee, a reward. It's
never asked about: each kind starts in Income or Interest & Fees, and goes wherever you
last moved one of its kind.
_Avoid_: bank payee

**Categorize**:
To give a transaction its category. A transaction waits in the inbox until it's
categorized, split or marked as a transfer.
_Avoid_: file, assign, approve

**Split**:
A transaction divided across several categories, each part with its own amount.

**Transfer**:
Money moving between two of your own accounts, however the bank moved it: paying a card
from chequing, savings to chequing, an e-transfer to your own account at another bank.

**E-transfer**:
Money sent to or received from someone else, a friend or an associate, through the
bank's e-transfer service. With your own account on the other end it's a transfer.
_Avoid_: transfer (on its own)

**Refund**:
Money back from the payee you paid, for that purchase. It goes back to the purchase's
category.

**Reimbursement**:
Someone else paying you back for something you paid: a share of a shared expense, an
employer's expense cheque. It goes back to the category the money left from, not to
Income.

**Shared expense**:
Something you paid that others chip in for, like a team fee or a roommate's half of the
power bill.
_Avoid_: bill (for this)

**Share**:
One person's part of a shared expense, usually sent by e-transfer.

## Inbox

**Inbox**:
The transactions the app isn't sure about, and any you sent back, each waiting to be
categorized, split or marked as a transfer.
_Avoid_: register, review queue

**Inbox item**:
One transaction shown in the inbox.
_Avoid_: card

**Group**:
Several inbox items from one payee, or the shares of one shared expense, shown and
categorized as one.

**Suggestion**:
A category the app offers for an inbox item, best first. It's only an offer: an inbox
item counts in no category until it's categorized.

**Always ask**:
A payee setting: each of its transactions is its own inbox item and is never categorized
without asking. It's on from the start for anyone you e-transfer with.

**Send back**:
To put a transaction back in the inbox yourself: clear its category, unsplit it, or say
it isn't a transfer. It waits there until you categorize it.
_Avoid_: uncategorize, reopen

**Re-check**:
The app looking again at everything waiting in the inbox (at launch, after an import,
after you categorize) and categorizing what it's now sure of. It never changes what you
decided.
_Avoid_: refile

## Budget

**Budget**:
Everything one copy of the app keeps: accounts, categories, payees and the money
assigned. Starting over means starting a new budget. The Budget page shows its
categories month by month.

**Dashboard**:
The page the app opens on: Ready to assign, the inbox, overspent categories, cash and
what's owed, and what each category spent month by month.
_Avoid_: Reports, home page

**Budget start**:
The first month the budget counts. Months before it still teach the app and count
towards Usual.

**Cash**:
The money in your open cash accounts, and in any closed one that still has money in it.
It isn't known until every open cash account has a balance. Ready to assign is checked
against it.

**Net worth**:
Cash minus what you owe on debt accounts, for the accounts in the app. A house, a
mortgage or investments aren't accounts, so they aren't in it.
_Avoid_: total, balance

**Category**:
A named pot of money for one purpose, like Food or Rent: the envelope of the envelope
method.
_Avoid_: envelope

**Hidden category**:
A category you no longer use. It keeps its history and money but isn't offered for new
transactions.
_Avoid_: archived, deleted

**Payment category**:
The category each debt account gets, holding money set aside to pay it. Card spending
from a funded category moves that money here.
_Avoid_: payment envelope

**Income**:
The category money coming in goes to. It adds to Ready to assign instead of being spent.

**Interest & Fees**:
The category for interest and bank fees charged on any account.

**Ready to assign**:
Cash that hasn't been given to a category yet. Money in that's still in the inbox isn't
counted until it's categorized; money out already is.
_Avoid_: To be budgeted, unassigned

**Assigned**:
Money given to a category for one month, taken from Ready to assign.
_Avoid_: allocated, funded

**Available**:
What's in a category now: what came in from last month, plus Assigned, less what was
spent.

**Spent**:
What a category's transactions came to in a month, refunds and reimbursements included.

**Budgeted**:
What you plan a category to cost each month. A plan, not money: nothing moves until
it's assigned.
_Avoid_: Each month, monthly amount, target, goal

**Usual**:
What a category actually spent on average in the 3, 6 or 12 whole months before a month,
counting from its first spending. Not a plan: that's Budgeted.
_Avoid_: Needs, average, estimate

**Above usual**:
A month in which a category spent clearly more than its Usual. It can still have money
left: that isn't overspending.
_Avoid_: overspent, over budget

**Overspending**:
A category below zero because cash spending went past what was in it. Card spending past
it is new debt instead. The Overspending setting says how it's paid for: This month, Next
month or Carry over.

**Cover**:
To move money into an overspent category until it's back to zero.

## Recurring charges

**Recurring charge**:
Something charged on a steady rhythm that the app tracks by name: a subscription or a
bill.
_Avoid_: subscription (for both kinds)

**Subscription**:
A recurring charge for a service, like a streaming plan.

**Bill**:
A recurring charge for something owed, like power, a phone plan or a payment plan.

**Price type**:
How a recurring charge's price may move: Same Price (to the cent), Same Price, Different
Currency (billed in US$, so it wobbles) or Fluctuates Every Time (any amount).

**Charged you again**:
The warning when a recurring charge shows up after it was cancelled or after its end
date.
