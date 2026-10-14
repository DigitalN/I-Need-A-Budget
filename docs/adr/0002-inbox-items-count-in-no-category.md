# Inbox items count in no category until they're categorized

An inbox item used to carry its top suggestion as a tentative category, so the Budget
page was roughly right before the inbox was cleared. The maintainer wants the opposite:
what's still in the inbox isn't on the Budget page yet. So an inbox item counts in no
category (Spent, Available, Usual, the Dashboard's spending) until it's categorized, and
suggestions only show in the inbox. Its money can't simply vanish, so: money out has
already left the bank and comes off Ready to assign; money in waits outside Ready to
assign, because it may turn out to be a reimbursement that belongs to a category.

## Considered options

- Money in and out both counting toward Ready to assign until categorized (how
  e-transfers worked): simpler, but a friend's $50 share of the power bill could be
  assigned elsewhere, and Ready to assign would go below zero when it's put back.

## Consequences

- The cash check under Ready to assign gets a third part: "= $X in Chequing − $Y in
  categories − $Z waiting in the Inbox".
- A card charge waiting in the inbox is set aside in the card's payment category as card
  spending not in a category yet, as before. Money in on a card (a refund) changes
  nothing until it's categorized: it isn't cash.
- Decided 2026-10-13, built in 1.1.0 (the upgrade to schema 5 clears the tentative categories).
