"""Turning bank descriptions into payees, and matching similar names."""

from payees import clean_merchant, describe, find_similar, merchant_key, person_key


def test_chains_lose_store_numbers_and_towns():
    for raw in ("MCDONALD'S #29159 CALGARY AB", "McDonalds 23921 Stony Plain AB", "MCDONALD'S #26129 QPS CALGARY A"):
        assert describe(raw, "", "credit_card").name == "McDonald's"
    assert describe("DQ GRILL & CHILL #2701 CALGARY", "", "credit_card").name == "Dairy Queen"
    assert describe("DAIRY QUEEN# 27303 STONY PLAIN A", "", "credit_card").name == "Dairy Queen"


def test_specific_brands_win_over_general_ones():
    assert describe("Amazon.ca Prime Member VANCOUVER", "", "credit_card").name == "Amazon Prime"
    assert describe("AMZN Mktp CA*5X2G880H3 TORONTO O", "", "credit_card").name == "Amazon"
    assert describe("COSTCO CANADA LIQUOR 5 SHERWOOD", "", "credit_card").name == "Costco Liquor"
    assert describe("GOOGLE *YouTubePremium HALIFAX N", "", "credit_card").name == "YouTube Premium"
    assert describe("TACO BELL #12 CALGARY AB", "", "credit_card").name == "Taco Bell"


def test_processor_prefixes_and_cut_off_lines():
    assert clean_merchant("SQ *CORNER BAKERY ROAS CALGARY") == "Corner Bakery Roas"
    assert clean_merchant("PRAIRIE HARDWARE LTD. CALGARY A") == "Prairie Hardware Ltd."
    assert clean_merchant("SP ROGUE WAVE COFFEE CALGARY AB") == "Rogue Wave Coffee"
    assert clean_merchant("LSP*Fox Burger Highlan Calgary") == "Fox Burger Highlan"
    assert clean_merchant("NEWSGROUP SERVICE WINTER PARK FL U") == "Newsgroup Service"
    assert clean_merchant("HUGHES (STORE#18) CALGARY AB") == "Hughes"
    assert clean_merchant("THE ART OF CAKE INC CALGARY AB") == "The Art of Cake Inc"


def test_a_town_at_the_start_of_a_name_stays():
    assert clean_merchant("CALGARY EPARK CALGARY AB") == "Calgary Epark"


def test_e_transfers_use_the_full_name_from_memo():
    d = describe("INTERAC e-Transfer From: JORDAN", "INTERAC e-Transfer From: JORDAN A SAMPLE", "chequing")
    assert (d.name, d.kind, d.key) == ("Jordan A Sample", "person", "person:jordanasample")
    d = describe("INTERAC e-Transfer To: Riley Exa", "INTERAC e-Transfer To: Riley Example", "chequing")
    assert d.name == "Riley Example"


def test_eft_names_keep_acronyms_and_drop_a_cut_off_letter():
    d = describe("EFT Withdrawal to ABC FInancial", "EFT Withdrawal to ABC FInancial L", "chequing")
    assert (d.name, d.kind) == ("ABC Financial", "organization")
    assert describe("EFT Deposit from SAMPLE LIFE", "EFT Deposit from SAMPLE LIFE", "chequing").name == "Sample Life"
    assert describe("EFT Withdrawal to EPCOR", "EFT Withdrawal to EPCOR", "chequing").name == "EPCOR"


def test_bank_rows_get_hints():
    assert describe("PAYMENT - THANK YOU", "", "credit_card").hint == "transfer"
    assert describe("Tangerine Credit Card Payment", "Tangerine Credit Card Payment", "chequing").hint == "transfer"
    assert describe("PURCHASE INTEREST", "", "credit_card").hint == "interest"
    assert describe("Interest Paid", "Interest Paid", "chequing").hint == "income"
    assert describe("MONEY-BACK REWARD", "", "credit_card").hint == "income"
    assert describe("Withdrawal - Scotiabank ABM", "Withdrawal - Scotiabank ABM", "chequing").hint == "cash"
    purchase = describe("Interac - Purchase - CAPILANO RE", "Interac - Purchase - CAPILANO REGISTRY CALGARY  AB  CA", "chequing")
    assert purchase.name == "Capilano Registry" and purchase.hint == ""


def test_similar_names_match_a_known_payee():
    known = [(merchant_key("Safeway"), 1, "Safeway"), (merchant_key("Burger King"), 2, "Burger King"),
             (merchant_key("Corner Bakery Roasters"), 3, "Corner Bakery Roasters")]
    saveway = describe("SAVEWAY 1532 CALGARY AB", "", "credit_card")
    assert find_similar(saveway.name, saveway.key, known)[:2] == (1, "Safeway")
    assert find_similar(saveway.name, saveway.key, known)[3] is True
    baron = describe("BURGER BARON CALGARY AB", "", "credit_card")
    assert find_similar(baron.name, baron.key, known) is None
    cut = describe("SQ *CORNER BAKERY ROAS CALGARY", "", "credit_card")
    assert find_similar(cut.name, cut.key, known)[0] == 3  # cut off at 32 characters


def test_people_match_with_a_middle_initial_and_ask_about_nicknames():
    known = [(person_key("Riley Example"), 7, "Riley Example"), (person_key("Zach Sample"), 8, "Zach Sample")]
    assert find_similar("Riley J Example", person_key("Riley J Example"), known)[::3] == (7, True)
    zachary = find_similar("Zachary Sample", person_key("Zachary Sample"), known)
    assert zachary[0] == 8 and zachary[3] is False  # close enough to ask, not to assume
    assert find_similar("Sam Other", person_key("Sam Other"), known) is None
