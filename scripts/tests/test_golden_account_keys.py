# SPDX-License-Identifier: GPL-3.0-only
"""Golden keys a budget keeps and finds again (issue #79). Made-up files and names only.

A file finds its account by keys kept in `account_sources` (an account number with its
bank, a Tangerine nickname, a set-up CSV's name with dates left out), a set-up CSV finds
its layout by the file's header (`csv_layouts.signature`), and an import keeps its kind of
file. Change how one is made and what a budget already holds is never found again: a file
for an account you have makes a new one, or a set-up CSV asks to be set up again. The
expected values are what the code returned when this was written, as literals; if one
fails, the change is the bug.
"""

import pytest

import banks
import csvlayout
import importer
from helpers import card_number, write
from test_golden_fingerprints import ROWS, quicken_file

# Every bank banks.py knows → the slug in its accounts' keys.
SLUGS = [
    ('Tangerine', 'tangerine'),
    ('RBC', 'rbc'),
    ('TD', 'td'),
    ('BMO', 'bmo'),
    ('Scotiabank', 'scotiabank'),
    ('CIBC', 'cibc'),
    ('National Bank', 'nationalbank'),
    ('ATB', 'atb'),
    ('Desjardins', 'desjardins'),
    ('Simplii Financial', 'simpliifinancial'),
    ('EQ Bank', 'eqbank'),
    ('PC Financial', 'pcfinancial'),
    ('Laurentian Bank', 'laurentianbank'),
    ('HSBC', 'hsbc'),
    ('American Express', 'americanexpress'),
    ('Capital One', 'capitalone'),
    ('MBNA', 'mbna'),
    ('Rogers Bank', 'rogersbank'),
    ('Neo Financial', 'neofinancial'),
    ('KOHO', 'koho'),
    ('Wealthsimple', 'wealthsimple'),
    ('Brim Financial', 'brimfinancial'),
    ('Motusbank', 'motusbank'),
    ('Manulife Bank', 'manulifebank'),
    ('Home Trust', 'hometrust'),
    ('Canadian Western Bank', 'canadianwesternbank'),
    ('Servus Credit Union', 'servuscreditunion'),
    ('Connect First Credit Union', 'connectfirstcreditunion'),
    ('Vancity', 'vancity'),
    ('Coast Capital Savings', 'coastcapitalsavings'),
    ('First West Credit Union', 'firstwestcreditunion'),
    ('Prospera Credit Union', 'prosperacreditunion'),
    ('Meridian Credit Union', 'meridiancreditunion'),
    ('Alterna Savings', 'alternasavings'),
    ('DUCA Credit Union', 'ducacreditunion'),
    ('Libro Credit Union', 'librocreditunion'),
    ('Affinity Credit Union', 'affinitycreditunion'),
    ('Conexus Credit Union', 'conexuscreditunion'),
    ('Steinbach Credit Union', 'steinbachcreditunion'),
    ('Cambrian Credit Union', 'cambriancreditunion'),
    ('Assiniboine Credit Union', 'assiniboinecreditunion'),
    ('UNI Financial', 'unifinancial'),
]

# What files call a bank → the slug, through canonical().
OTHER_NAMES = [
    ('ING Direct', 'tangerine'),
    ('ING Bank of Canada', 'tangerine'),
    ('Tangerine Bank', 'tangerine'),
    ('Royal Bank', 'rbc'),
    ('Royal Bank of Canada', 'rbc'),
    ('RBC Royal Bank', 'rbc'),
    ('RBC Banque Royale', 'rbc'),
    ('TD Canada Trust', 'td'),
    ('Toronto-Dominion Bank', 'td'),
    ('TD Bank', 'td'),
    ('Bank of Montreal', 'bmo'),
    ('BMO Bank of Montreal', 'bmo'),
    ('BMO Banque de Montréal', 'bmo'),
    ('Bank of Nova Scotia', 'scotiabank'),
    ('The Bank of Nova Scotia', 'scotiabank'),
    ('Scotia', 'scotiabank'),
    ('Canadian Imperial Bank of Commerce', 'cibc'),
    ('National Bank of Canada', 'nationalbank'),
    ('Banque Nationale', 'nationalbank'),
    ('NBC', 'nationalbank'),
    ('ATB Financial', 'atb'),
    ('Alberta Treasury Branches', 'atb'),
    ('Caisse Desjardins', 'desjardins'),
    ('Mouvement Desjardins', 'desjardins'),
    ('Simplii', 'simpliifinancial'),
    ('Equitable Bank', 'eqbank'),
    ("President's Choice Financial", 'pcfinancial'),
    ('PC Money', 'pcfinancial'),
    ('Banque Laurentienne', 'laurentianbank'),
    ('HSBC Bank Canada', 'hsbc'),
    ('Amex', 'americanexpress'),
    ('Neo', 'neofinancial'),
    ('Brim', 'brimfinancial'),
    ('motus bank', 'motusbank'),
    ('CWB', 'canadianwesternbank'),
    ('Servus', 'servuscreditunion'),
    ('Connect First', 'connectfirstcreditunion'),
    ('Vancouver City Savings Credit Union', 'vancity'),
    ('Coast Capital', 'coastcapitalsavings'),
    ('First West', 'firstwestcreditunion'),
    ('Prospera', 'prosperacreditunion'),
    ('Meridian', 'meridiancreditunion'),
    ('Alterna', 'alternasavings'),
    ('DUCA', 'ducacreditunion'),
    ('Libro', 'librocreditunion'),
    ('Affinity', 'affinitycreditunion'),
    ('Conexus', 'conexuscreditunion'),
    ('Cambrian', 'cambriancreditunion'),
    ('UNI Coopération financière', 'unifinancial'),
    # Spacing and case don't matter; a bank banks.py doesn't know keeps its own name.
    ('ROYAL BANK OF CANADA', 'rbc'),
    ('  rbc royal  bank ', 'rbc'),
    ('Sample Credit Union', 'samplecreditunion'),
    ('', ''),
    (None, ''),
]


@pytest.mark.parametrize("name, slug", SLUGS + OTHER_NAMES)
def test_bank_slugs(name, slug):
    assert banks.slug(name) == slug


def test_every_bank_has_a_golden_slug():
    """A bank added to banks.py gets its rows above too."""
    assert [shown for shown, _ in SLUGS] == list(banks._NAMES)
    assert {name for name, _ in OTHER_NAMES} >= {other for others in banks._NAMES.values() for other in others}


def test_account_number_keys():
    assert importer.account_number_key("Royal Bank of Canada", "4321") == "number:rbc:4321"
    assert importer.account_number_key("Sample Credit Union", "6789") == "number:samplecreditunion:6789"
    assert importer.account_number_key("", "1234") == "number:unknown:1234"
    assert importer.full_number_key("TD Canada Trust", "123456789") == "number-full:td:123456789"


ACCOUNTS = [
    # (file name, bank, the file's account block, keys, version 1.0's keys, kind)
    ("statement.qfx", "Sample Credit Union",
     "<BANKACCTFROM><BANKID>0000<ACCTID>000123456789<ACCTTYPE>CHECKING</BANKACCTFROM>",
     ["number-full:samplecreditunion:000123456789", "number:samplecreditunion:6789"], ["ofx:chequing:6789"], "chequing"),
    ("statement.ofx", "Royal Bank of Canada",
     "<BANKACCTFROM><BANKID>0003<ACCTID>00000-0004321<ACCTTYPE>SAVINGS</BANKACCTFROM>",
     ["number-full:rbc:000000004321", "number:rbc:4321"], ["ofx:savings:4321"], "savings"),
    ("statement.qbo", "TD Canada Trust",
     f"<CCACCTFROM><ACCTID>{card_number('8765')}</CCACCTFROM>",
     [f"number-full:td:{card_number('8765')}", "number:td:8765"], [], "credit_card"),
    # Tangerine says SAVINGS for chequing too: the file name decides.
    ("Chequing.QFX", "Tangerine",
     "<BANKACCTFROM><BANKID>0614<ACCTID>000111222333<ACCTTYPE>SAVINGS</BANKACCTFROM>",
     ["number-full:tangerine:000111222333", "number:tangerine:2333", "tangerine-bank:chequing"], ["ofx:chequing:2333"],
     "chequing"),
    # Tangerine's Mastercard comes as a credit line with the card number.
    ("x.QFX", "Tangerine",
     f"<BANKACCTFROM><BANKID>0614<ACCTID>{card_number('1234')}<ACCTTYPE>CREDITLINE</BANKACCTFROM>",
     [f"number-full:tangerine:{card_number('1234')}", "number:tangerine:1234", "tangerine-card:1234", "tangerine-card"], [],
     "credit_card"),
    ("x.ofx", "",
     "<BANKACCTFROM><BANKID>0000<ACCTID>98765<ACCTTYPE>CHECKING</BANKACCTFROM>",
     ["number-full:unknown:98765", "number:unknown:8765"], ["ofx:chequing:8765"], "chequing"),
]


@pytest.mark.parametrize("filename, bank, account, keys, legacy, kind", ACCOUNTS, ids=[a[0] + "|" + a[1] for a in ACCOUNTS])
def test_a_quicken_files_account_keys(tmp_path, filename, bank, account, keys, legacy, kind):
    [part] = importer.parse(write(tmp_path, filename, quicken_file(bank, account, ROWS))).parts
    assert (part.account.keys, part.account.legacy_keys, part.account.kind) == (keys, legacy, kind)


def test_an_rbc_csvs_account_keys(tmp_path):
    path = write(tmp_path, "csv12345.csv",
                 '"Account Type","Account Number","Transaction Date","Cheque Number","Description 1","Description 2",'
                 '"CAD$","USD$"\n'
                 'Chequing,00000-0004321,10/1/2026,,"e-Transfer sent","SAMPLE PERSON 4XYZ12",-40.00,,\n'
                 'Visa,0000000000008765,10/2/2026,,"SAMPLE CAFE CALGARY AB","",-7.25,,\n')
    chequing, visa = importer.parse(path).parts
    assert chequing.account.keys == ["number-full:rbc:000000004321", "number:rbc:4321"]
    assert visa.account.keys == ["number-full:rbc:0000000000008765", "number:rbc:8765"]


def test_tangerine_csvs_account_keys(tmp_path):
    from helpers import CARD_FILE, bank_csv, card_csv
    card = write(tmp_path, CARD_FILE, card_csv([("10/03/2026", "SAMPLE CAFE CALGARY AB", "-4.50", "")]))
    assert importer.parse(card).parts[0].account.keys == ["tangerine-card:1234", "tangerine-card"]
    rows = bank_csv([("10/01/2026", "SAMPLE CAFE CALGARY AB", "", "-4.50")])
    for filename, key in [("Chequing.CSV", "tangerine-bank:chequing"), ("chequing-2.csv", "tangerine-bank:chequing"),
                          ("Savings Account (1).CSV", "tangerine-bank:savings account"),
                          ("My Line of Credit.CSV", "tangerine-bank:my line of credit"),
                          ("Travel Fund.csv", "tangerine-bank:travel fund")]:
        assert importer.parse(write(tmp_path, filename, rows)).parts[0].account.keys == [key]


def test_set_up_csv_account_keys():
    """A set-up CSV's account is found by its layout and its file name, dates and repeat numbers left out."""
    for filename, key in [("Visa transactions 2026-10-13 (1).csv", "csv:7:visa transactions"),
                          ("accountactivity.csv", "csv:7:accountactivity"),
                          ("Sample CU Chequing 10-13-2026.CSV", "csv:7:sample cu chequing"),
                          ("Visa 2026.10.13.csv", "csv:7:visa"),
                          ("export_20261013.csv", "csv:7:export"),
                          ("2026-10-13 (1).csv", "csv:7:file"),
                          # A browser's or macOS's repeat number
                          ("accountactivity-2.csv", "csv:7:accountactivity"),
                          ("accountactivity (3).csv", "csv:7:accountactivity"),
                          ("accountactivity 2.csv", "csv:7:accountactivity"),
                          # Numbers that aren't dates
                          ("Account 12-345.csv", "csv:7:account 12"),
                          ("Acct 1-23-456.csv", "csv:7:acct 1 23"),
                          # Issue #177: dates the repeat-number rule took the end of, or that weren't taken out at all
                          ("Visa transactions 2026-10-13.csv", "csv:7:visa transactions"),
                          ("Visa transactions 2026-10-13-2.csv", "csv:7:visa transactions"),
                          ("Visa 10-13-26.csv", "csv:7:visa"),
                          ("Visa 26-10-13.csv", "csv:7:visa"),
                          ("2026-10-13.csv", "csv:7:file"),
                          ("Visa 2026-10.csv", "csv:7:visa"),
                          ("export_202610.csv", "csv:7:export"),
                          ("Visa Oct 13 2026.csv", "csv:7:visa"),
                          ("Visa October 13, 2026.csv", "csv:7:visa"),
                          ("Visa 13-Oct-2026.csv", "csv:7:visa"),
                          ("Statement_Oct_2026.csv", "csv:7:statement"),
                          ("transactions-2026-10-13T14-32-05.csv", "csv:7:transactions"),
                          ("Account 7 2026-10-13.csv", "csv:7:account 7"),
                          # A year alone isn't a date
                          ("Visa 2026.csv", "csv:7:visa 2026")]:
        assert importer.csv_account_key(7, filename) == key


def test_set_up_csv_layout_signatures():
    with_header = csvlayout.sniff("Date,Description,Amount,Balance\n2026-10-01,SAMPLE CAFE CALGARY AB,-4.50,995.50\n"
                                  "2026-10-02,SAMPLE GROCER,-61.20,934.30\n2026-10-03,SAMPLE EMPLOYER,1500.00,2434.30\n")
    assert csvlayout.signature(with_header) == csvlayout.legacy_signature(with_header) == "h:date|description|amount|balance"
    # Without a header: an empty column (no deposits this month) counts as money (the empty-column rule); layouts saved before it are found by legacy_signature().
    no_header = csvlayout.sniff("2026-10-01,SAMPLE CAFE CALGARY AB,-4.50,,995.50\n2026-10-02,SAMPLE GROCER,-61.20,,934.30\n"
                                "2026-10-03,SAMPLE EMPLOYER,,1500.00,2434.30\n")
    assert csvlayout.signature(no_header) == "n:5:dtmmm"
    assert csvlayout.signature({"header": None, "width": 4, "types": ["d", "t", "m", "e"]}) == "n:4:dtmm"
    assert csvlayout.legacy_signature({"header": None, "width": 4, "types": ["d", "t", "m", "e"]}) == "n:4:dtme"


def test_a_set_up_csvs_kind_of_file():
    layout = {"id": 7, "has_header": True, "date_col": 0, "date_order": "ymd", "desc_cols": [1], "amount_col": 2,
              "out_col": None, "in_col": None, "negate": False}
    assert importer.layout_kind(layout) == "csv:7:40823269"
