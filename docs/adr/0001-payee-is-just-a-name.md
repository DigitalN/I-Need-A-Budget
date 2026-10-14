# A payee is just a name; e-transfers are guarded by Always ask

Payees had a kind (merchant, organization, person, bank), and "person" carried the rule
that an e-transfer is never categorized without asking, because money sent to one name
goes to different things each time. That rule is about how the money moved, not who it
went to, and the bank's text can't tell a friend from a landlord's company anyway. So a
payee is just a name. Anyone you e-transfer with starts with Always ask on, which keeps
the old rule by default; switching it off for one payee (rent e-transferred to a landlord)
lets the app learn it and track it as a bill, with no special feature for that.

## Consequences

- What was person-only belongs to the e-transfer itself: no provisional category (since
  [ADR 0002](0002-inbox-items-count-in-no-category.md) no inbox item has one; an
  e-transfer is also never in the inbox's "Accept all"), and
  money in is matched to shares of a shared expense rather than to refunds.
- Bank entries (interest, fees, rewards) are the one exception: they're never asked
  about and have no Always ask switch. Each kind starts in Income or Interest & Fees and
  then goes wherever you last moved one of its kind. Cash withdrawals and deposits are
  ordinary transactions, not bank entries.
- Decided 2026-10-13, built in 1.0.9. Being an e-transfer is kept on the transaction
  (`hint = 'etransfer'`); the upgrade marked the transactions of payees that were people
  and switched Always ask on for those payees. The old `payees.kind` column stays, only
  to keep bank wording out of similar-name matching.
