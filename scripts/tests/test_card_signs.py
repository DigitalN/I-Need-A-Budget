# SPDX-License-Identifier: GPL-3.0-only
"""Cards and lines of credit: which sign means "you owe", in a bank's file and in a balance you
type. Made-up data only (card numbers built in code)."""

from pathlib import Path

from helpers import call, card_number, write

PAGE = (Path(__file__).resolve().parents[2] / "Application Files" / "index.html").read_text(encoding="utf-8")


def covers(start, as_of):
    """The days a made-up Quicken file says it covers, written as a bank writes them (local midnight); '' when
    `start` is None (the file doesn't say)."""
    return f"<DTSTART>{start}000000.000[-5:EST]<DTEND>{as_of}235959.000[-5:EST]\n" if start else ""


def card_ofx(rows, balance, as_of="20261010", acct_id=None, trntype="OTHER", start=None):
    """A made-up credit card OFX download. rows: (YYYYMMDD, amount, name, fitid). `start`: the first day the
    file says it covers (DTSTART, YYYYMMDD), if it says."""
    trn = "".join(f"<STMTTRN><TRNTYPE>{trntype if isinstance(trntype, str) else trntype[i]}<DTPOSTED>{d}<TRNAMT>{a}"
                  f"<FITID>{f}<NAME>{n}</STMTTRN>\n" for i, (d, a, n, f) in enumerate(rows))
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>Sample Bank<FID>00000</FI></SONRS>"
            "</SIGNONMSGSRSV1><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>CAD\n"
            f"<CCACCTFROM><ACCTID>{acct_id or card_number('4444')}</CCACCTFROM>\n<BANKTRANLIST>\n{covers(start, as_of)}{trn}"
            "</BANKTRANLIST>"
            f"<LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>\n")


def _import(app, text, name="visa.ofx"):
    return call(app, "import_files", [str(write(app.test_downloads, name, text))])["results"][0]


def _card(app):
    return next(a for a in call(app, "accounts") if a["is_debt"])


def _amounts(app):
    return sorted(t["amount"] for t in call(app, "transactions", {})["items"])


PURCHASES = [("20261003", "-30.00", "SAMPLE ARCADE", "A1"), ("20261005", "-45.50", "SAMPLE HOTEL", "A2"),
             ("20261008", "100.00", "PAYMENT - THANK YOU", "A3")]


def test_a_positive_balance_with_negative_charges_can_mean_money_owed(app):
    """Issue #60: a card LEDGERBAL of 120.50 showed as owing -$120.50 everywhere. Some banks do write it
    that way, most mean a card in your favour: with no balance to go by (issue #119) the first file asks."""
    app.db.set_meta("onboarded", "1")
    path = str(write(app.test_downloads, "visa.ofx", card_ofx(PURCHASES, "120.50")))
    assert call(app, "import_files", [path])["needs_setup"][0]["needs"] == "balance_sign"
    result = call(app, "answer_balance", path, "owed")["results"][0]
    assert _card(app)["balance"] == -12050
    assert "owe" in result["balance_note"]
    assert _amounts(app) == [-4550, -3000, 10000]


def test_a_negative_balance_with_negative_charges_is_left_alone(app):
    result = _import(app, card_ofx(PURCHASES, "-120.50"))
    assert _card(app)["balance"] == -12050
    assert "flipped" not in result["balance_note"]


def test_a_card_that_writes_purchases_as_positive_is_flipped(app):
    """Issue #60: purchases imported as money in (refunds), the balance with them."""
    inverted = [("20261003", "30.00", "SAMPLE ARCADE", "A1"), ("20261005", "45.50", "SAMPLE HOTEL", "A2"),
                ("20261006", "12.00", "SAMPLE TAXI", "A4"), ("20261008", "-100.00", "PAYMENT - THANK YOU", "A3")]
    result = _import(app, card_ofx(inverted, "0.00"))
    assert _amounts(app) == [-4550, -3000, -1200, 10000]
    assert "flipped" in result["balance_note"].lower()


def test_trntype_decides_when_it_says_what_a_row_is(app):
    inverted = [("20261003", "30.00", "SAMPLE ONE", "A1"), ("20261005", "45.50", "SAMPLE TWO", "A2"),
                ("20261008", "-100.00", "SAMPLE THREE", "A3")]
    _import(app, card_ofx(inverted, "75.50", trntype=["DEBIT", "DEBIT", "CREDIT"]))
    assert _amounts(app) == [-4550, -3000, 10000]
    assert _card(app)["balance"] == -7550


def test_a_card_in_credit_can_be_set_by_hand(app):
    card = call(app, "add_account", "Sample Visa", "credit_card")
    call(app, "set_balance", card, 2000, "2026-10-01")
    assert _card(app)["balance"] == -2000  # what you owe, the default
    call(app, "set_balance", account_id=card, cents=2000, date="2026-10-02", in_favour=True)
    assert _card(app)["balance"] == 2000  # $20.00 in your favour
    other = call(app, "add_account", name="Sample Line", kind="line_of_credit", balance=5000, date="2026-10-01", in_favour=True)
    assert next(a for a in call(app, "accounts") if a["id"] == other)["balance"] == 5000
    chequing = call(app, "add_account", "Sample Chequing", "chequing", 7000, "2026-10-01")
    assert next(a for a in call(app, "accounts") if a["id"] == chequing)["balance"] == 7000


def test_check_balance_can_say_in_your_favour_too(app):
    card = call(app, "add_account", "Sample Visa", "credit_card", 2000, "2026-10-01")
    out = call(app, "reconcile", account_id=card, cents=500, in_favour=True)
    assert out["adjusted"] == 2500 and _card(app)["balance"] == 500


def test_the_page_offers_in_your_favour_wherever_a_card_balance_is_typed():
    assert PAGE.count("favourBox(") >= 5  # the Inbox notice, Accounts, Check balance, Add an account, onboarding
    for call_text in ("call('set_balance', { account_id: acct, cents, in_favour: inFavour(acct) })",
                      "call('reconcile', { account_id: id, cents, in_favour: a.is_debt && inFavour('rec') })",
                      "in_favour: ['line_of_credit', 'credit_card'].includes($('#na-kind').value) && inFavour('new')",
                      "in_favour: inFavour(el.dataset.obBalance)"):
        assert call_text in PAGE
