# A transfer with an account kept by hand gets its other half from the app

A transfer has two halves, one in each account's file, and the app waits for the second
file to pair them. The Wallet never gets a file, and neither does any account you keep by
hand. So marking a $100.00 cash withdrawal from chequing as a transfer to the Wallet left
it half a transfer for good: the budget counted nothing, the Wallet was $100.00 short, and
the check line under Ready to assign stayed "$100.00 off" (issue #75). Now, when you mark a
transfer with an account no file has ever brought transactions to, and there's no other
half there within the pairing window, the app adds it: a transaction added by hand, paired,
and yours, because you marked the transfer.

## Considered options

- Ask first ("Add $100.00 to the Wallet?"): the maintainer chose no question. Marking the
  transfer already says it, and one ⌘Z removes both halves.
- Only the Wallet (kind `cash`): an account at another bank you keep by hand has the same
  problem, so it's any account no file has brought transactions to (`ledger.kept_by_hand`:
  no import, no row from a file, and no file would find it).
- Leave it as it was: half a transfer and the check line off until you added the other
  half yourself.

## Consequences

- Accounts that get files still wait for their file: the bank's row is the other half.
- The added half only exists because of the transfer. Whatever ends the transfer takes it
  away: Not a transfer from either side, a category or a split on the other half, marking
  that one with another account, deleting it or removing its import, or a merge that puts
  both halves in one account. It's marked by its hint (`added_half`), as a balance
  adjustment is by its own, since `reason` is never read back.
- Deciding about the added half itself (a category, a split, another other half) makes it
  an ordinary transaction of yours, which stays.
- If the account later gets a file, the bank's row of the same amount within three days
  takes the added half's place, as it does for any transaction added by hand, and keeps the
  transfer. It's the bank's from then on.
- A balance adjustment is never the other half, so marking a transfer never pairs with one.
- The added half is often an empty Wallet's first transaction. No download can be missing from
  an account kept by hand, so its first transaction doesn't hold Usual back, whenever the
  account was added (issue #83's test for a new account; decided 2026-11-09). Otherwise a
  Wallet made while setting up would hide every category's Usual until a month had passed.
- Only transfers you mark: an import's own guess that a row is a transfer to an account
  kept by hand still waits for its other half.
- Decided 2026-11-08, built for 1.4.0.
