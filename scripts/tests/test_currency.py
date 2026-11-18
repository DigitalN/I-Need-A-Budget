# SPDX-License-Identifier: GPL-3.0-only
"""One currency per budget (issue #76): the first file that says a currency sets it, and a file all in another
currency is refused. Made-up data only (card numbers built in code)."""

import logging
import os
import time

import csvlayout
import importer
from helpers import CARD_FILE, bank_csv, call, card_csv, card_number, write


def rows(n, tag, day=1, amount="-10.00"):
    """n made-up transactions from Oct `day` on: (YYYYMMDD, amount, name, fitid)."""
    return [(f"202610{day + i:02d}", amount, f"SAMPLE STORE {tag}{i}", f"{tag}{i}") for i in range(n)]


def statement(acct_id, txns, currency="CAD", acct="CHECKING", balance="100.00", as_of="20261010", card=False,
              orig=None):
    """One account's statement. `orig`: a currency every row says it was bought in (ORIGCURRENCY and CURRENCY)."""
    extra = f"<ORIGCURRENCY><CURRATE>1.36<CURSYM>{orig}</ORIGCURRENCY><CURRENCY><CURRATE>1.36<CURSYM>{orig}" \
            "</CURRENCY>" if orig else ""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}{extra}</STMTTRN>\n"
                  for d, a, n, f in txns)
    cur = f"<CURDEF>{currency}\n" if currency else ""
    if card:
        return (f"<CCSTMTTRNRS><CCSTMTRS>{cur}<CCACCTFROM><ACCTID>{acct_id}</CCACCTFROM>\n<BANKTRANLIST>\n{trn}"
                f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></CCSTMTRS></CCSTMTTRNRS>")
    return (f"<STMTTRNRS><STMTRS>{cur}<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>{acct}</BANKACCTFROM>\n"
            f"<BANKTRANLIST>\n{trn}</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL>"
            "</STMTRS></STMTTRNRS>")


def ofx(*statements):
    """A made-up Quicken file from SAMPLE BANK with these statements."""
    bank = "".join(s for s in statements if s.startswith("<STMTTRNRS>"))
    cards = "".join(s for s in statements if s.startswith("<CCSTMTTRNRS>"))
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>SAMPLE BANK<FID>00000"
            "</FI></SONRS></SIGNONMSGSRSV1>" + (f"<BANKMSGSRSV1>{bank}</BANKMSGSRSV1>" if bank else "") +
            (f"<CREDITCARDMSGSRSV1>{cards}</CREDITCARDMSGSRSV1>" if cards else "") + "</OFX>\n")


CHEQUING, SAVINGS, US_ACCOUNT = "000111222333", "000444555666", "000999888777"
REFUSED = "This file is in USD; your budget is in CAD."


def imports(app, *files):
    return call(app, "import_files", [str(write(app.test_downloads, name, text)) for name, text in files])


def currency(app):
    return app.db.get_meta("currency")


def account_names(app):
    return sorted(a["name"] for a in call(app, "accounts"))


def cad_budget(app):
    """A budget in CAD: its first file, a chequing account's, says so."""
    imports(app, ("Chequing.qfx", ofx(statement(CHEQUING, rows(3, "C")))))
    assert currency(app) == "CAD"


# ── setting it ───────────────────────────────────────────────

def test_onboarding_with_us_dollar_files_makes_a_usd_budget_that_refuses_a_cad_file(app):
    out = imports(app, ("Checking.qfx", ofx(statement(CHEQUING, rows(3, "C"), "USD"))),
                  ("Savings.qfx", ofx(statement(SAVINGS, rows(2, "S"), "USD", acct="SAVINGS"))))
    assert len(out["results"]) == 2 and out["errors"] == []
    assert currency(app) == "USD"
    path = write(app.test_downloads, "Other.qfx", ofx(statement(US_ACCOUNT, rows(2, "O"), "CAD")))
    out = call(app, "import_files", [str(path)])
    assert out["results"] == []
    assert [(e["filename"], e["error"]) for e in out["errors"]] == [("Other.qfx", "This file is in CAD; your budget is in USD.")]
    assert len(call(app, "accounts")) == 2  # no account for it
    assert path.exists()  # left where it was
    assert currency(app) == "USD"  # it never changes


def test_files_that_say_no_currency_leave_none_and_always_fit(app):
    """Tangerine's CSVs say no currency: the budget has none until a file says one, then CSVs still fit."""
    imports(app, ("Chequing.CSV", bank_csv([("10/01/2026", "SAMPLE CAFE", "SAMPLE CAFE CALGARY AB", "-4.50")])),
            (CARD_FILE, card_csv([("10/02/2026", "SAMPLE DINER CALGARY AB", "-12.00", "Restaurant")])))
    assert currency(app) is None
    imports(app, ("US Account.qfx", ofx(statement(US_ACCOUNT, rows(2, "U"), "USD"))))
    assert currency(app) == "USD"
    out = imports(app, ("Chequing.CSV", bank_csv([("10/05/2026", "SAMPLE BAKERY", "SAMPLE BAKERY CALGARY AB", "-6.00")])))
    assert [r["added"] for r in out["results"]] == [1] and out["errors"] == []
    added = call(app, "add_transaction", call(app, "accounts")[0]["id"], "2026-10-06", "SAMPLE MARKET", -300)
    assert added and currency(app) == "USD"


def test_most_accounts_decide_and_the_others_are_refused(app):
    """Chequing (CAD, 5 transactions), Mastercard (CAD, 3), US Dollar (USD, 2) → CAD; US Dollar is refused."""
    out = imports(app, ("Chequing.qfx", ofx(statement(CHEQUING, rows(5, "C")))),
                  ("Mastercard.qfx", ofx(statement(card_number("4444"), rows(3, "M"), card=True))),
                  ("US Dollar.qfx", ofx(statement(US_ACCOUNT, rows(2, "U"), "USD"))))
    assert currency(app) == "CAD"
    assert len(out["results"]) == 2
    assert [(e["filename"], e["error"]) for e in out["errors"]] == [("US Dollar.qfx", REFUSED)]


def test_most_accounts_in_one_file_decide_and_the_other_is_skipped_with_a_note(app):
    out = imports(app, ("All.qfx", ofx(statement(CHEQUING, rows(5, "C")), statement(US_ACCOUNT, rows(2, "U"), "USD"),
                                       statement(card_number("4444"), rows(3, "M"), card=True))))
    assert currency(app) == "CAD"
    assert len(out["results"]) == 2 and out["errors"] == []
    assert [(s["filename"], s["account"], s["currency"], s["budget"]) for s in out["skipped"]] == \
        [("All.qfx", "SAMPLE BANK Chequing ••8777", "USD", "CAD")]


def test_an_account_counts_once_and_a_tie_goes_to_more_transactions(app):
    """Two downloads of one USD account count once: one CAD account against one USD account, and the CAD one has
    more transactions."""
    out = imports(app, ("Chequing.qfx", ofx(statement(CHEQUING, rows(5, "C")))),
                  ("US 1.qfx", ofx(statement(US_ACCOUNT, rows(3, "U"), "USD"))),
                  ("US 2.qfx", ofx(statement(US_ACCOUNT, rows(3, "U", day=4), "USD"))))
    assert currency(app) == "CAD"
    assert len(out["results"]) == 1 and len(out["errors"]) == 2


def test_a_tie_goes_to_the_account_with_more_transactions(app):
    out = imports(app, ("Chequing.qfx", ofx(statement(CHEQUING, rows(2, "C")))),
                  ("US.qfx", ofx(statement(US_ACCOUNT, rows(4, "U"), "USD"))))
    assert currency(app) == "USD"
    assert [(e["filename"], e["error"]) for e in out["errors"]] == [("Chequing.qfx", "This file is in CAD; your budget is in USD.")]


def test_most_said():
    assert importer.most_said([("CAD", 300), ("CAD", 120), ("USD", 15)]) == "CAD"
    assert importer.most_said([("CAD", 2), ("USD", 4)]) == "USD"
    assert importer.most_said([("USD", 3), ("CAD", 3)]) == "CAD"  # always the same
    assert importer.most_said([]) is None


# ── RBC ──────────────────────────────────────────────────────

RBC_HEADER = '"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2","CAD$","USD$"\n'
RBC_CAD = ('Chequing,00000-0004321,10/1/2026,,"Payroll Deposit","SAMPLE EMPLOYER",1500.00,\n'
           'Chequing,00000-0004321,10/3/2026,,"Interac purchase","SAMPLE GROCER",-61.20,\n')
RBC_USD = ('US Dollar,00000-0005678,10/2/2026,,"Transfer in","",,200.00\n'
           'US Dollar,00000-0005678,10/4/2026,,"SAMPLE STORE","",,-15.00\n')


def test_rbc_cad_and_usd_accounts_in_a_cad_budget(app, caplog):
    cad_budget(app)
    with caplog.at_level(logging.INFO, logger="inab"):
        out = imports(app, ("csv12345.csv", RBC_HEADER + RBC_CAD + RBC_USD))
    assert [(r["account_name"], r["added"]) for r in out["results"]] == [("RBC Chequing", 2)]
    assert [(s["filename"], s["account"]) for s in out["skipped"]] == [("csv12345.csv", "RBC Chequing ••5678")]
    assert "csv12345.csv: RBC Chequing ••5678 skipped: it's in USD; your budget is in CAD" in caplog.text
    assert not any(a["last4"] == "5678" for a in call(app, "accounts"))
    assert not any("skipped" in (i["balance_note"] or "") for i in call(app, "imports"))  # not in Imported files


def test_an_rbc_file_all_in_usd_is_refused_in_a_cad_budget(app):
    cad_budget(app)
    out = imports(app, ("csv12345.csv", RBC_HEADER + RBC_USD))
    assert out["results"] == [] and [e["error"] for e in out["errors"]] == [REFUSED]
    assert account_names(app) == ["SAMPLE BANK Chequing"]


def test_an_rbc_row_with_both_columns_is_read_from_the_accounts_column(app):
    path = write(app.test_downloads, "csv12345.csv", RBC_HEADER + RBC_CAD + RBC_USD +
                 'Chequing,00000-0004321,10/5/2026,,"SAMPLE SHOP","",-27.00,-20.00\n'
                 'US Dollar,00000-0005678,10/6/2026,,"SAMPLE SHOP","",-27.00,-20.00\n')
    cad, usd = importer.parse(path).parts
    assert (cad.currency, usd.currency) == ("CAD", "USD")
    assert [r.amount for r in cad.rows] == [150000, -6120, -2700]
    assert [r.amount for r in usd.rows] == [20000, -1500, -2000]


# ── OFX ──────────────────────────────────────────────────────

def test_a_usd_statement_in_a_file_with_several_is_skipped_and_never_asked_about(app):
    """In a CAD budget, set up: the CAD statement imports, and the USD card (a new account, balance above zero) is
    neither asked "new account?" nor "is it what you owe?"."""
    cad_budget(app)
    imports(app, ("Card.qfx", ofx(statement(card_number("4444"), rows(2, "M"), card=True, balance="-20.00"))))
    app.db.set_meta("onboarded", "1")
    out = imports(app, ("Both.qfx", ofx(statement(CHEQUING, rows(2, "D", day=5)),
                                        statement(card_number("9999"), rows(2, "V"), "USD", card=True, balance="120.50"),
                                        statement(US_ACCOUNT, rows(2, "U"), "USD"))))
    assert out["needs_setup"] == [] and out["errors"] == []
    assert [r["added"] for r in out["results"]] == [2]
    assert sorted(s["account"] for s in out["skipped"]) == ["SAMPLE BANK Chequing ••8777", "SAMPLE BANK card ••9999"]
    assert len(call(app, "accounts")) == 2


def test_a_card_statement_in_usd_is_refused_in_a_cad_budget(app):
    cad_budget(app)
    out = imports(app, ("Card.qfx", ofx(statement(card_number("4444"), rows(2, "M"), "USD", card=True))))
    assert out["results"] == [] and [e["error"] for e in out["errors"]] == [REFUSED]


def test_rows_bought_in_another_currency_dont_change_the_statements(app):
    out = imports(app, ("Card.qfx", ofx(statement(card_number("4444"), rows(2, "M", amount="-13.60"), card=True,
                                                  orig="USD"))))
    assert currency(app) == "CAD"
    assert [r["added"] for r in out["results"]] == [2]
    assert {t["amount"] for t in call(app, "transactions", {})["items"]} == {-1360}
    assert importer._ofx_currency("<STMTRS><BANKTRANLIST><STMTTRN><CURDEF>USD</STMTTRN></BANKTRANLIST>") is None


# ── set-up CSVs ──────────────────────────────────────────────

def usd_csv(codes=("USD", "USD", "USD"), header="Currency"):
    lines = [f"Date,Description,Amount,{header}"] + [f"2026-10-0{i + 1},SAMPLE SHOP {i},-{i + 1}2.00,{code}"
                                                      for i, code in enumerate(codes)]
    return "\n".join(lines) + "\n"


def test_a_csvs_currency_column(app):
    sniff = lambda text: csvlayout.sniff(text)  # noqa: E731
    assert csvlayout.currency(sniff(usd_csv())) == "USD"
    assert csvlayout.currency(sniff(usd_csv(header="Devise"))) == "USD"
    assert csvlayout.currency(sniff(usd_csv(("CAD", "USD", "CAD")))) is None  # mixed: says nothing
    assert csvlayout.currency(sniff(usd_csv(("", "USD", "")))) is None  # a card's original currency of US purchases
    assert csvlayout.currency(sniff(usd_csv(header="Original Currency"))) is None  # not headed exactly that


def test_a_csv_all_in_usd_is_refused_before_its_set_up(app):
    cad_budget(app)
    path = write(app.test_downloads, "Visa.csv", usd_csv())
    out = call(app, "import_files", [str(path)])
    assert out["needs_setup"] == [] and [e["error"] for e in out["errors"]] == [REFUSED]
    assert app.dispatch("csv_setup", [str(path)]) == {"ok": False, "error": REFUSED}
    layout = {"has_header": True, "date_col": 0, "date_order": "ymd", "desc_cols": [1], "amount_col": 2,
              "out_col": None, "in_col": None, "negate": False}
    got = app.dispatch("setup_csv", [str(path), layout, {"name": "Visa", "kind": "credit_card", "bank": ""}])
    assert got == {"ok": False, "error": REFUSED}
    with app.db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM csv_layouts").fetchone()[0] == 0
    assert account_names(app) == ["SAMPLE BANK Chequing"]


def test_a_csv_with_mixed_codes_is_set_up_as_usual(app):
    cad_budget(app)
    path = write(app.test_downloads, "Visa.csv", usd_csv(("CAD", "USD", "CAD")))
    out = call(app, "import_files", [str(path)])
    assert out["errors"] == [] and [f["needs"] for f in out["needs_setup"]] == ["layout"]


def test_a_set_up_csv_that_says_its_currency_sets_the_budgets(app):
    path = write(app.test_downloads, "Visa.csv", usd_csv())
    layout = {"has_header": True, "date_col": 0, "date_order": "ymd", "desc_cols": [1], "amount_col": 2,
              "out_col": None, "in_col": None, "negate": False}
    call(app, "setup_csv", str(path), layout, {"name": "Visa", "kind": "chequing", "bank": ""})
    assert currency(app) == "USD"


# ── Downloads ────────────────────────────────────────────────

def test_a_file_from_downloads_in_another_currency_waits_in_the_inbox(app):
    cad_budget(app)
    imports(app, ("Card.qfx", ofx(statement(card_number("4444"), rows(2, "M"), card=True, balance="-20.00"))))
    app.db.set_meta("onboarded", "1")
    path = write(app.test_downloads, "Savings USD.qfx",
                 ofx(statement(card_number("9999"), rows(2, "V"), "USD", card=True, balance="120.50")))
    os.utime(path, (time.time() - 10, time.time() - 10))
    out = app.watch_downloads()
    assert out["auto"] and out["results"] == [] and out["needs_setup"] == []
    assert [(e["filename"], e["currency"], e["budget"]) for e in out["errors"]] == [("Savings USD.qfx", "USD", "CAD")]
    [waiting] = app.pending_files()
    assert (waiting["filename"], waiting["needs"], waiting["currency"], waiting["budget"]) == \
        ("Savings USD.qfx", "currency", "USD", "CAD")  # never "new_account" or "balance_sign"
    assert call(app, "inbox")["files"][0]["needs"] == "currency"
    inbox_before = call(app, "counts")["inbox"]
    call(app, "ignore_file", str(path))
    assert app.pending_files() == [] and call(app, "counts")["inbox"] == inbox_before - 1
    app._seen_files = {}  # as at the next launch
    assert app.watch_downloads() is None  # you said not to import it: no toast again
    assert path.exists() and len(call(app, "accounts")) == 2


def test_a_file_you_said_not_to_import_never_decides_the_currency(app):
    """No currency yet, set up with a card CSV: a USD card file in Downloads you said Don't import to (as in 1.4.2)
    has no say at the next launch. A CAD file arriving beside it then sets CAD, and the ignored one is refused, with
    no toast."""
    imports(app, (CARD_FILE, card_csv([("10/02/2026", "SAMPLE DINER CALGARY AB", "-12.00", "Restaurant")])))
    app.db.set_meta("onboarded", "1")
    path = write(app.test_downloads, "US Card.qfx",
                 ofx(statement(card_number("9999"), rows(4, "V"), "USD", card=True, balance="-40.00")))
    call(app, "ignore_file", str(path))
    os.utime(path, (time.time() - 10, time.time() - 10))
    assert app.watch_downloads() is None
    assert currency(app) is None and path.exists()
    chequing = write(app.test_downloads, "Chequing.qfx", ofx(statement(CHEQUING, rows(2, "C"))))
    os.utime(chequing, (time.time() - 10, time.time() - 10))
    app._seen_files = {}  # as at the next launch
    out = app.watch_downloads()
    assert currency(app) == "CAD"
    assert [r["filename"] for r in out["results"]] == ["Chequing.qfx"] and out["errors"] == []
    assert out["needs_setup"] == [] and app.pending_files() == [] and path.exists()


# ── a budget from before 1.4.3 ───────────────────────────────

def as_before_143(app):
    with app.db.write(keep_undo=True) as c:
        c.execute("DELETE FROM meta WHERE key IN ('currency', 'currency_read')")


def test_a_new_budget_has_nothing_to_read(app):
    assert app.db.get_meta("currency_read") == "1"
    assert app.read_currency() is None and currency(app) is None


def test_an_old_budget_whose_files_say_usd_is_in_usd(app):
    imports(app, ("Checking.qfx", ofx(statement(CHEQUING, rows(3, "C"), "USD"))),
            ("Savings.qfx", ofx(statement(SAVINGS, rows(2, "S"), "USD", acct="SAVINGS"))))
    as_before_143(app)
    assert app.read_currency() == "USD"
    assert currency(app) == "USD" and app.db.get_meta("currency_read") == "1"


def test_an_old_budget_whose_files_say_none_has_no_currency_yet(app):
    imports(app, ("Chequing.CSV", bank_csv([("10/01/2026", "SAMPLE CAFE", "SAMPLE CAFE CALGARY AB", "-4.50")])))
    as_before_143(app)
    assert app.read_currency() is None
    assert currency(app) is None and app.db.get_meta("currency_read") == "1"
    imports(app, ("US.qfx", ofx(statement(US_ACCOUNT, rows(2, "U"), "USD"))))  # the next file that says one sets it
    assert currency(app) == "USD"


def test_an_old_budget_with_mixed_files_takes_most_and_logs_the_others_once(app, caplog):
    """Made before the check: one CAD account and two USD ones."""
    for name, text in (("Chequing.qfx", ofx(statement(CHEQUING, rows(3, "C")))),
                       ("Savings.qfx", ofx(statement(SAVINGS, rows(2, "S"), "USD", acct="SAVINGS"))),
                       ("US.qfx", ofx(statement(US_ACCOUNT, rows(2, "U"), "USD")))):
        as_before_143(app)
        imports(app, (name, text))
    as_before_143(app)
    with caplog.at_level(logging.INFO, logger="inab"):
        assert app.read_currency() == "USD"
    assert "SAMPLE BANK Chequing: its files say CAD; the budget is in USD" in caplog.text
    with app.db.write(keep_undo=True) as c:
        c.execute("DELETE FROM meta WHERE key = 'currency'")
    assert app.read_currency() is None and currency(app) is None  # only once
    out = imports(app, ("Chequing.qfx", ofx(statement(CHEQUING, rows(2, "D", day=5)))))
    assert currency(app) == "CAD" and len(out["results"]) == 1


# ── the review's cases ───────────────────────────────────────

def test_a_cad_code_written_cdn_is_cad(app):
    assert csvlayout.currency(csvlayout.sniff(usd_csv(("CDN", "cdn", "CDN")))) == "CAD"
    cad_budget(app)
    imports(app, ("Card.qfx", ofx(statement(card_number("4444"), rows(2, "M"), "CDN", card=True))))
    assert len(call(app, "accounts")) == 2


def test_a_csv_saying_another_currency_in_the_import_that_sets_it_is_refused(app):
    """No currency yet: a CAD QFX and a CSV (not set up) whose Currency column says USD come in together."""
    out = imports(app, ("Chequing.qfx", ofx(statement(CHEQUING, rows(3, "C")))), ("Visa.csv", usd_csv()))
    assert currency(app) == "CAD"
    assert out["needs_setup"] == [] and [(e["filename"], e["error"]) for e in out["errors"]] == [("Visa.csv", REFUSED)]


def test_the_currency_is_set_even_when_the_files_of_it_wait_on_a_question(app):
    """No currency yet, set up: a USD card file (a new card, so "new account?") and a CAD file come in together.
    One account each, and the USD one has more transactions: the budget is in USD straight away, though its file
    waits on its question, so the CAD file is refused (decided on #76's review). Answering it imports it."""
    imports(app, (CARD_FILE, card_csv([("10/02/2026", "SAMPLE DINER CALGARY AB", "-12.00", "Restaurant")])))
    app.db.set_meta("onboarded", "1")
    out = imports(app, ("US Card.qfx", ofx(statement(card_number("9999"), rows(4, "V"), "USD", card=True,
                                                     balance="-40.00"))),
                  ("Chequing.qfx", ofx(statement(CHEQUING, rows(2, "C")))))
    assert currency(app) == "USD"
    assert [f["filename"] for f in out["needs_setup"]] == ["US Card.qfx"] and out["results"] == []
    assert [(e["filename"], e["error"]) for e in out["errors"]] == \
        [("Chequing.qfx", "This file is in CAD; your budget is in USD.")]
    path = str(app.test_downloads / "US Card.qfx")
    out = call(app, "set_file_accounts", path, [{"index": 0, "name": "US Card"}])
    assert [r["added"] for r in out["results"]] == [4]


def test_a_file_of_the_few_never_sets_the_currency_while_the_others_wait_on_a_question(app):
    """No currency yet, set up with a card CSV: two new CAD cards (each asks "new account?") and a USD bank account
    (asks nothing) come in together. The budget is in CAD, the USD file is refused, and both cards import once
    their question is answered."""
    imports(app, (CARD_FILE, card_csv([("10/02/2026", "SAMPLE DINER CALGARY AB", "-12.00", "Restaurant")])))
    app.db.set_meta("onboarded", "1")
    out = imports(app, ("Card A.qfx", ofx(statement(card_number("1111"), rows(2, "A"), card=True, balance="-20.00"))),
                  ("Card B.qfx", ofx(statement(card_number("2222"), rows(2, "B"), card=True, balance="-20.00"))),
                  ("US.qfx", ofx(statement(US_ACCOUNT, rows(3, "U"), "USD"))))
    assert currency(app) == "CAD"
    assert sorted(f["filename"] for f in out["needs_setup"]) == ["Card A.qfx", "Card B.qfx"]
    assert out["results"] == [] and [(e["filename"], e["error"]) for e in out["errors"]] == [("US.qfx", REFUSED)]
    for name in ("Card A", "Card B"):
        out = call(app, "set_file_accounts", str(app.test_downloads / f"{name}.qfx"), [{"index": 0, "name": name}])
        assert [r["added"] for r in out["results"]] == [2] and out["errors"] == []
    assert currency(app) == "CAD"


def test_a_file_in_the_other_currency_is_refused_at_once_never_asked_about(app):
    """No currency yet, set up: a new USD card (it would ask "new account?") beside a CAD chequing file that's most
    of them. The USD card is refused in the same import, never put to the page as a question."""
    imports(app, (CARD_FILE, card_csv([("10/02/2026", "SAMPLE DINER CALGARY AB", "-12.00", "Restaurant")])))
    app.db.set_meta("onboarded", "1")
    out = imports(app, ("US Card.qfx", ofx(statement(card_number("9999"), rows(2, "V", day=1), "USD", card=True,
                                                     balance="-20.00"))),
                  ("Chequing.qfx", ofx(statement(CHEQUING, rows(5, "C", day=3)))))
    assert currency(app) == "CAD"
    assert out["needs_setup"] == []
    assert [(e["filename"], e["error"]) for e in out["errors"]] == [("US Card.qfx", REFUSED)]
    assert [r["filename"] for r in out["results"]] == ["Chequing.qfx"]


def test_the_inbox_looks_again_once_the_currency_is_set(app):
    """A file waiting in Downloads as "new account?" before the budget had a currency is a file in another currency
    once it has one."""
    imports(app, ("Chequing.CSV", bank_csv([("10/01/2026", "SAMPLE CAFE", "SAMPLE CAFE CALGARY AB", "-4.50")])))
    app.db.set_meta("onboarded", "1")
    write(app.test_downloads, "US.qfx", ofx(statement(US_ACCOUNT, rows(2, "U"), "USD")))
    assert [f["needs"] for f in app.pending_files()] == ["new_account"]
    other = app.test_downloads.parent / "dropped"
    other.mkdir()
    dropped = write(other, "Card.qfx", ofx(statement(card_number("4444"), rows(2, "M"), card=True, balance="-20.00")))
    call(app, "import_files", [str(dropped)])
    assert currency(app) == "CAD"
    assert [(f["filename"], f["needs"]) for f in app.pending_files()] == [("US.qfx", "currency")]


def test_part_numbers_stay_in_step_when_an_account_before_it_is_skipped(app):
    """A file whose first statement is in USD (skipped) and whose second is a new CAD card: asked "new account?"
    about the card, then imported into the account picked."""
    cad_budget(app)
    imports(app, ("Card.qfx", ofx(statement(card_number("4444"), rows(2, "M"), card=True, balance="-20.00"))))
    app.db.set_meta("onboarded", "1")
    out = imports(app, ("Both.qfx", ofx(statement(US_ACCOUNT, rows(2, "U"), "USD"),
                                        statement(card_number("9999"), rows(2, "V"), card=True, balance="-20.00"))))
    [waiting] = out["needs_setup"]
    assert waiting["needs"] == "new_account"
    [part] = call(app, "file_accounts", waiting["path"])["parts"]
    assert (part["index"], part["last4"]) == (0, "9999")
    card_id = next(a["id"] for a in call(app, "accounts") if a["last4"] == "4444")
    out = call(app, "set_file_accounts", waiting["path"], [{"index": 0, "account_id": card_id}])
    assert [(r["account_id"], r["added"]) for r in out["results"]] == [(card_id, 2)]
    assert [s["account"] for s in out["skipped"]] == ["SAMPLE BANK Chequing ••8777"]


def test_onboarding_lists_a_file_in_another_currency_and_says_why(app):
    cad_budget(app)
    write(app.test_downloads, "US.qfx", ofx(statement(US_ACCOUNT, rows(2, "U"), "USD")))
    [listed] = call(app, "scan_downloads")["files"]
    assert (listed["filename"], listed["refused"], listed["error"]) == ("US.qfx", True, REFUSED)
