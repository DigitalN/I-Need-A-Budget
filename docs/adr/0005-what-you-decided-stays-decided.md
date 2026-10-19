# What you decided stays decided

The app re-checks the inbox at launch, after imports and after you categorize, and it
pairs a transfer's halves whenever one arrives. Neither knew what you had decided: the
only record was the words "Categorized by you" in a transaction's reason, and "Not a
transfer" or sending a transaction back to the inbox weren't recorded at all. So the
next launch undid them: a −$300.00 chequing payment you marked Not a transfer and put in
Food was paired again with the card's +$300.00 and lost Food, and a charge you sent back
to look at again was categorized straight back. Now who decided is kept on the
transaction (`by_you`), and nothing the app does on its own changes a transaction you
decided. One module, `inbox.py`, puts transactions in the inbox and takes them out, so
this is enforced in one place.

## Considered options

- Keep reading the reason text, and add more phrases for Not a transfer and sending
  back: no upgrade needed, but every rule stays a string match spread over several
  modules, and a reworded reason silently breaks one.
- Let a transfer's other half still pair with a transaction you categorized, since that's
  usually right: the app would still be overriding a choice you made. The other half
  waits in the inbox instead, saying which transaction it looks like the other half of,
  and Transfer pairs them.

## Consequences

- Yours: what you categorize (one, a group, Accept all), split, mark as a transfer or Not
  a transfer, send back, or add by hand. The app's: what an import or the re-check
  categorizes ("also categorized 3 more like it"), and what removing an import or a
  balance adjustment from Reconcile sends to the inbox.
- Not a transfer sends both halves back, and the app never pairs them again.
- A transaction you sent back is its own inbox item, outside its payee's group and
  outside Accept all, still with suggestions.
- The re-check only changes inbox items that aren't yours, and never a recurring charge's
  price: that follows a charge only when it first arrives, or when you choose "New price
  for…".
- A category change outside the inbox changes only that transaction: no re-check of its
  payee (a mixed payee like a warehouse store, food one week and household the next,
  must stay as you put it). In the inbox, categorizing one still re-checks its payee
  unless you chose One at a time.
- "Charged you again" and "Matched to … (similar name)" are worked out each time from the
  recurring charge and the payee match, not kept as text. `reason` is only text to show;
  no code reads it.
- A category or account that has been used can only be hidden or closed (1.1.4), so
  nothing you decided years ago comes back to the inbox.
- Decided 2026-10-19, to build in 1.2.0. The upgrade to schema 6 marks as yours every
  transaction whose reason is "Categorized by you", "Filed by you", "Split by you" or
  "Added by you", and every inbox item whose reason is "No longer a transfer" or empty
  (sent back). A transfer marked by hand can't be told from one the app paired, so it
  stays the app's.
