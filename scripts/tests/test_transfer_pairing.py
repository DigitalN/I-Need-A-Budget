"""Pairing the two halves of a transfer, and guessing the account of a half whose other half
isn't imported (another bank's wording: "ONLINE BANKING TRANSFER"). Made-up data only."""

from helpers import call, write


def ofx(rows, balance, as_of, acct_id="000111222333", org="SAMPLE BANK", acct="CHECKING"):
    """A made-up OFX download. rows: (YYYYMMDD, amount, name, fitid)"""
    trn = "".join(f"<STMTTRN><TRNTYPE>OTHER<DTPOSTED>{d}<TRNAMT>{a}<FITID>{f}<NAME>{n}</STMTTRN>\n" for d, a, n, f in rows)
    return ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\n\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>" + org +
            "<FID>00000</FI></SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CAD\n"
            f"<BANKACCTFROM><BANKID>0000<ACCTID>{acct_id}<ACCTTYPE>{acct}</BANKACCTFROM>\n<BANKTRANLIST>\n{trn}"
            f"</BANKTRANLIST><LEDGERBAL><BALAMT>{balance}<DTASOF>{as_of}</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n")


def _txn(app, amount):
    return next(x for x in call(app, "transactions", {})["items"] if x["amount"] == amount)


def _chequing_with_a_transfer(app, name="ONLINE BANKING TRANSFER - 7788", amount="-500.00"):
    f = write(app.test_downloads, "chq.ofx", ofx([("20261001", "2000.00", "PAYROLL DEPOSIT SAMPLE CO", "B1"),
                                                  ("20261002", amount, name, "B2")], "1500.00", "20261003"))
    call(app, "import_files", [str(f)])


def test_a_transfer_to_an_account_you_dont_have_isnt_a_card_payment(app):
    """Issue #51: with one card in the app, a transfer to savings (not in the app) became a
    payment to the card: done, never in the Inbox, and the card's payment category dropped."""
    call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    _chequing_with_a_transfer(app)
    t = _txn(app, -50000)
    assert t["status"] == "review" and t["transfer_account_id"] is None


def test_a_card_payment_from_chequing_with_one_card_is_still_guessed(app):
    visa = call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    _chequing_with_a_transfer(app, name="CREDIT CARD PAYMENT", amount="-120.00")
    t = _txn(app, -12000)
    assert (t["status"], t["transfer_account_id"]) == ("done", visa)


def test_a_transfer_naming_an_account_you_have_goes_to_it(app):
    """"TO SAVINGS", and a savings account is in the app though its file isn't imported yet."""
    call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    savings = call(app, "add_account", "Sample Savings", "savings", 0, "2026-09-30")
    _chequing_with_a_transfer(app, name="INTERNET TRANSFER TO SAVINGS", amount="-200.00")
    t = _txn(app, -20000)
    assert (t["status"], t["transfer_account_id"]) == ("done", savings)


def test_the_savings_half_arriving_later_pairs_with_the_waiting_chequing_half(app):
    call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    _chequing_with_a_transfer(app, name="INTERNET TRANSFER TO SAVINGS", amount="-200.00")
    waiting = _txn(app, -20000)
    assert waiting["status"] == "review"
    f = write(app.test_downloads, "sav.ofx", ofx([("20261003", "200.00", "INTERNET TRANSFER FROM CHEQUING", "S1")],
                                                 "200.00", "20261004", acct_id="000999888777", acct="SAVINGS"))
    call(app, "import_files", [str(f)])
    chequing_half, savings_half = _txn(app, -20000), _txn(app, 20000)
    assert chequing_half["transfer_id"] == savings_half["id"] and savings_half["transfer_id"] == chequing_half["id"]
    assert chequing_half["status"] == savings_half["status"] == "done"
    waiting = {i["id"] for i in call(app, "inbox")["items"]}
    assert chequing_half["id"] not in waiting and savings_half["id"] not in waiting


def test_a_guessed_half_is_paired_again_when_its_real_counterpart_arrives(app):
    """Chequing's half was guessed as a payment to the card (its wording named the card: one
    cash account, one card). The savings half then arrives: the guess gives way to it."""
    visa = call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    _chequing_with_a_transfer(app, name="CREDIT CARD PAYMENT", amount="-200.00")
    assert _txn(app, -20000)["transfer_account_id"] == visa
    f = write(app.test_downloads, "sav.ofx", ofx([("20261003", "200.00", "INTERNET TRANSFER FROM CHEQUING", "S1")],
                                                 "200.00", "20261004", acct_id="000999888777", acct="SAVINGS"))
    call(app, "import_files", [str(f)])
    chequing_half, savings_half = _txn(app, -20000), _txn(app, 20000)
    assert chequing_half["transfer_id"] == savings_half["id"]
    assert chequing_half["transfer_account_id"] == savings_half["account_id"]


def test_a_savings_deposit_doesnt_pair_with_a_categorized_card_purchase(app):
    """Issue #51: a $50.00 card purchase filed in Fun Money lost its category and became a
    "transfer" because the savings deposit looked like one."""
    cats = {c["name"]: c["id"] for g in call(app, "categories") for c in g["categories"]}
    fun = cats.get("Fun Money") or call(app, "add_category", call(app, "add_group", "Fun"), "Fun Money")
    call(app, "add_account", "Sample Visa", "credit_card", 0, "2026-09-30")
    visa_file = write(app.test_downloads, "visa.ofx", ofx([("20261004", "-50.00", "SAMPLE ARCADE", "V1")], "50.00",
                                                          "20261005", acct_id="000555444333", acct="CREDITLINE"))
    call(app, "import_files", [str(visa_file)])
    purchase = _txn(app, -5000)
    with app.db.write() as c:  # as the app itself would have filed it from what it's learnt
        c.execute("UPDATE transactions SET category_id = ?, status = 'done', by_you = 0 WHERE id = ?", (fun, purchase["id"]))
    f = write(app.test_downloads, "sav.ofx", ofx([("20261004", "50.00", "INTERNET TRANSFER FROM CHEQUING", "S1")],
                                                 "50.00", "20261005", acct_id="000999888777", acct="SAVINGS"))
    call(app, "import_files", [str(f)])
    purchase, deposit = _txn(app, -5000), _txn(app, 5000)
    assert purchase["category_id"] == fun and purchase["transfer_id"] is None and purchase["transfer_account_id"] is None
    assert deposit["transfer_id"] is None and deposit["status"] == "review"
