"""Turning what the bank wrote into a payee: "SQ *CORNER BAKERY ROAS CALGARY" → "Corner Bakery Roas".

Pure functions, no database. `describe()` reads one bank row and returns a clean
display name, a stable matching key, what kind of payee it is, and a hint about what
the row is (a card payment, interest, a reward...). `find_similar()` matches a new
key against known ones, so "SAFEWAY #6432" and a misspelt "SAVEWAY 1532" both end up
as Safeway.

Tangerine quirks handled here:
- Card rows are cut at 32 characters, often mid-word ("TORONTO O", "SAN FRANC").
- Chequing rows cut the Name at 32 characters too, but repeat it in full in Memo.
- Card processors prefix the merchant ("SQ *", "SP ", "LSP*", "PAYPAL *").
- Chains add store numbers and the city ("McDonalds 12345 Red Deer AB").

Only well-known chains belong in BRANDS. Never put anything personal in this file
(people's names, local businesses someone uses, employers): the repository is meant
to hold no private data at all.
"""

import re
from dataclasses import dataclass

PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
    "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC",
    "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
COUNTRIES = {"CAN", "US", "USA", "DEU", "BEL", "GBR", "UK", "IRL", "FRA", "NLD", "CHE", "AUS", "DUB", "LUX"}
LOCATION_CODES = PROVINCES | STATES | COUNTRIES

CITIES = {
    # Canada
    "TORONTO", "MONTREAL", "VANCOUVER", "CALGARY", "EDMONTON", "OTTAWA", "WINNIPEG", "QUEBEC", "HAMILTON",
    "KITCHENER", "WATERLOO", "LONDON", "VICTORIA", "HALIFAX", "OSHAWA", "WINDSOR", "SASKATOON", "REGINA",
    "ST JOHNS", "KELOWNA", "BARRIE", "SHERBROOKE", "GUELPH", "ABBOTSFORD", "KINGSTON", "MISSISSAUGA",
    "BRAMPTON", "MARKHAM", "VAUGHAN", "RICHMOND", "RICHMOND HILL", "BURNABY", "SURREY", "OAKVILLE",
    "BURLINGTON", "LAVAL", "GATINEAU", "LONGUEUIL", "RED DEER", "LETHBRIDGE", "MEDICINE HAT",
    "GRANDE PRAIRIE", "ST ALBERT", "ST. ALBERT", "SHERWOOD PARK", "SHERWOOD", "SPRUCE GROVE", "STONY PLAIN",
    "LEDUC", "NISKU", "FORT SASKATCHEWAN", "AIRDRIE", "COCHRANE", "OKOTOKS", "CANMORE", "BANFF", "JASPER",
    "FORT MCMURRAY", "NORTH VANCOUVER", "WEST VANCOUVER", "COQUITLAM", "LANGLEY", "NANAIMO", "KAMLOOPS",
    "PRINCE GEORGE", "MONCTON", "FREDERICTON", "SAINT JOHN", "CHARLOTTETOWN", "WHITEHORSE", "YELLOWKNIFE",
    "IQALUIT", "THUNDER BAY", "SUDBURY", "SAULT STE MARIE", "PETERBOROUGH", "BELLEVILLE", "NIAGARA FALLS",
    "ST CATHARINES", "CAMBRIDGE", "BRANTFORD", "ORANGEVILLE", "NORTH YORK", "SCARBOROUGH", "ETOBICOKE",
    "THORNHILL", "WHITBY", "AJAX", "PICKERING", "MILTON", "NEWMARKET", "AURORA", "BEAUMONT", "DEVON",
    "CAMROSE", "WETASKIWIN", "HINTON", "EDSON", "LLOYDMINSTER", "BROOKS", "HIGH RIVER", "STRATHMORE",
    "CHESTERMERE", "SYLVAN LAKE", "OLDS", "DIDSBURY", "MORINVILLE", "GIBBONS", "BON ACCORD", "HAY LAKES",
    "WINTER PARK",
    # Where online services bill from
    "SAN FRANCISCO", "SEATTLE", "BELLEVUE", "MOUNTAIN VIEW", "NEW YORK", "LOS ANGELES", "CUPERTINO",
    "PALO ALTO", "REDMOND", "MENLO PARK", "SUNNYVALE", "SAN JOSE", "SAN MATEO", "AUSTIN", "BOSTON",
    "CHICAGO", "MADISON", "DUBLIN", "LUXEMBOURG", "AMSTERDAM", "GENEVA", "HANNOVER", "BERLIN", "BRUSSELS",
    "PARIS", "STOCKHOLM", "ZURICH", "CHESTNUT MOUNTAIN", "MONTREAL QC",
}

# (pattern on the upper-cased description, payee name). First match wins, so specific
# ones go first. Well-known chains and services only.
BRANDS = [
    (r"AMAZON\.CA PRIME|AMAZON PRIME|PRIME MEMBER|PRIME VIDEO", "Amazon Prime"),
    (r"KINDLE", "Kindle"),
    (r"AUDIBLE", "Audible"),
    (r"AMZN|AMAZON", "Amazon"),
    (r"COSTCO.*LIQUOR", "Costco Liquor"),
    (r"COSTCO.*GAS", "Costco Gas"),
    (r"COSTCO", "Costco"),
    (r"YOUTUBE ?PREMIUM|YOUTUBEPREMIUM", "YouTube Premium"),
    (r"YOUTUBE ?TV", "YouTube TV"),
    (r"GOOGLE ?\*? ?(STORAGE|GOOGLE ONE)", "Google One"),
    (r"APPLE\.COM|ITUNES", "Apple"),
    (r"MSBILL|MICROSOFT", "Microsoft"),
    (r"NETFLIX", "Netflix"),
    (r"SPOTIFY", "Spotify"),
    (r"DISNEY ?PLUS|DISNEYPLUS", "Disney+"),
    (r"\bCRAVE\b", "Crave"),
    (r"PARAMOUNT", "Paramount+"),
    (r"STEAMGAMES|STEAM PURCHASE|\bSTEAM\b", "Steam"),
    (r"NINTENDO", "Nintendo"),
    (r"PLAYSTATION|SONY INTERACTIVE", "PlayStation"),
    (r"\bXBOX\b", "Xbox"),
    (r"DISCORD", "Discord"),
    (r"OPENAI|CHATGPT", "ChatGPT"),
    (r"ANTHROPIC|CLAUDE\.AI", "Claude"),
    (r"1PASSWORD", "1Password"),
    (r"PROTON ?AG|PROTONMAIL|PROTON\.ME", "Proton"),
    (r"TUTAO|\bTUTA\b", "Tuta"),
    (r"DROPBOX", "Dropbox"),
    (r"ADOBE", "Adobe"),
    (r"GITHUB", "GitHub"),
    (r"PATREON", "Patreon"),
    (r"TWITCH", "Twitch"),
    (r"UBER ?EATS|UBEREATS", "Uber Eats"),
    (r"\bUBER\b", "Uber"),
    (r"SKIP ?THE ?DISHES", "SkipTheDishes"),
    (r"DOORDASH", "DoorDash"),
    (r"\bLYFT\b", "Lyft"),
    (r"MCDONALD", "McDonald's"),
    (r"DAIRY QUEEN|\bDQ\b", "Dairy Queen"),
    (r"TIM HORTON", "Tim Hortons"),
    (r"STARBUCKS", "Starbucks"),
    (r"\bA ?& ?W\b", "A&W"),
    (r"WENDY'?S", "Wendy's"),
    (r"BURGER KING", "Burger King"),
    (r"\bSUBWAY\b", "Subway"),
    (r"DOMINO'?S", "Domino's"),
    (r"PIZZA HUT", "Pizza Hut"),
    (r"BOSTON PIZZA", "Boston Pizza"),
    (r"PIZZA 73", "Pizza 73"),
    (r"PANAGO", "Panago"),
    (r"TACO BELL", "Taco Bell"),
    (r"\bKFC\b", "KFC"),
    (r"POPEYES", "Popeyes"),
    (r"CHIPOTLE", "Chipotle"),
    (r"FIVE GUYS", "Five Guys"),
    (r"HARVEY'?S", "Harvey's"),
    (r"MUCHO BURRITO", "Mucho Burrito"),
    (r"BARBURRITO", "Barburrito"),
    (r"MARY BROWN", "Mary Brown's"),
    (r"SECOND CUP", "Second Cup"),
    (r"SAVE ON FOODS|SAVE-ON-FOODS|SAVEONFOODS", "Save-On-Foods"),
    (r"SAFEWAY", "Safeway"),
    (r"SOBEYS", "Sobeys"),
    (r"SUPERSTORE|\bRCSS\b", "Real Canadian Superstore"),
    (r"NO ?FRILLS", "No Frills"),
    (r"LOBLAW", "Loblaws"),
    (r"FRESHCO", "FreshCo"),
    (r"WALMART|WAL-MART", "Walmart"),
    (r"T ?& ?T SUPERMARKET", "T&T Supermarket"),
    (r"SHOPPERS DRUG|^SDM\b", "Shoppers Drug Mart"),
    (r"LONDON DRUGS", "London Drugs"),
    (r"REXALL", "Rexall"),
    (r"CANADIAN TIRE", "Canadian Tire"),
    (r"HOME DEPOT", "Home Depot"),
    (r"LOWE'?S", "Lowe's"),
    (r"\bRONA\b", "Rona"),
    (r"\bIKEA\b", "IKEA"),
    (r"BEST BUY", "Best Buy"),
    (r"STAPLES", "Staples"),
    (r"DOLLARAMA", "Dollarama"),
    (r"WINNERS", "Winners"),
    (r"SPORT CHEK", "Sport Chek"),
    (r"MOUNTAIN EQUIPMENT|\bMEC\b", "MEC"),
    (r"PRINCESS AUTO", "Princess Auto"),
    (r"PETSMART", "PetSmart"),
    (r"PET VALU", "Pet Valu"),
    (r"PETRO-?CANADA|PETRO CAN", "Petro-Canada"),
    (r"\bESSO\b", "Esso"),
    (r"\bSHELL\b", "Shell"),
    (r"HUSKY", "Husky"),
    (r"\bCO-?OP\b", "Co-op"),
    (r"7-?ELEVEN", "7-Eleven"),
    (r"CIRCLE K", "Circle K"),
    (r"TESLA", "Tesla"),
    (r"ROCK ?AUTO", "RockAuto"),
    (r"TELUS", "Telus"),
    (r"ROGERS", "Rogers"),
    (r"\bBELL\b", "Bell"),
    (r"\bSHAW\b", "Shaw"),
    (r"FREEDOM MOBILE", "Freedom Mobile"),
    (r"KOODO", "Koodo"),
    (r"\bFIDO\b", "Fido"),
    (r"PUBLIC MOBILE", "Public Mobile"),
    (r"EPCOR", "EPCOR"),
    (r"ENMAX", "ENMAX"),
    (r"\bATCO\b", "ATCO"),
    (r"DIRECT ENERGY", "Direct Energy"),
    (r"SONNET", "Sonnet Insurance"),
    (r"INTACT INS", "Intact Insurance"),
    (r"RBC ?INS", "RBC Insurance"),
    (r"AFFIRM", "Affirm"),
    (r"PAYBRIGHT", "PayBright"),
    (r"KLARNA", "Klarna"),
    (r"\bETSY\b", "Etsy"),
    (r"\bEBAY\b", "eBay"),
    (r"ALIEXPRESS", "AliExpress"),
    (r"\bTEMU\b", "Temu"),
    (r"CINEPLEX", "Cineplex"),
    (r"TICKETMASTER", "Ticketmaster"),
    (r"SIMPLEFIN", "SimpleFIN"),
    (r"NEWSGROUP ?NINJA", "Newsgroup Ninja"),
    (r"MCMASTER", "McMaster-Carr"),
]
_BRANDS = [(re.compile(p), name) for p, name in BRANDS]

# Card-processor prefixes in front of the merchant's own name.
_PROCESSOR = re.compile(
    r"^(?:SQ ?\*|SP ?\*|LSP ?\*|TST ?\*|PP ?\*|PAYPAL ?\*|IC ?\*|DD ?\*|CKO ?\*|ZTL ?\*|BT ?\*|FS ?\*|"
    r"GOOGLE ?\*|GGL ?\*|WPY ?\*|PY ?\*|SP |LS |LSP )\s*")
# Lower-case inside a name ("The Art of Cake"). Not "a": it's usually a middle initial.
_SMALL_WORDS = {"of", "and", "the", "an", "in", "on", "at", "for", "de", "du", "la", "le", "et", "to"}


@dataclass
class Description:
    name: str        # what to call the payee
    key: str         # stable key for matching; see merchant_key()
    kind: str        # merchant | person | organization | bank
    hint: str = ""   # transfer | interest | income | cash | fee | ''
    brand: bool = False


def merchant_key(name):
    """Lower-case letters and digits only: 'McDonald's' → 'mcdonalds'."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def person_key(name):
    return "person:" + merchant_key(name)


def brand_of(text):
    upper = str(text).upper()
    for pattern, name in _BRANDS:
        if pattern.search(upper):
            return name
    return None


def title_case(text):
    words = []
    for i, word in enumerate(str(text).split()):
        lower = word.lower()
        if i > 0 and lower in _SMALL_WORDS:
            words.append(lower)
        elif any(ch.isdigit() for ch in word) or ("&" in word and len(word) <= 4):
            words.append(word.upper())
        else:
            words.append("-".join(p[:1].upper() + p[1:].lower() for p in word.split("-")))
    return " ".join(words)


def _looks_like_acronym(word):
    """RBC, CIBC, MSP: short and nearly vowel-free."""
    letters = re.sub(r"[^A-Za-z]", "", word)
    return 0 < len(letters) <= 4 and sum(ch in "AEIOUYaeiouy" for ch in letters) <= 1 and word.upper() == word


def _is_store_number(token):
    t = token.strip("()")
    if re.fullmatch(r"#?[A-Z]{0,2}\d[\d\-]*", t):
        return True
    if re.search(r"#\d", t) or sum(ch.isdigit() for ch in t) >= 4:
        return True
    return t in {"-", "#", "@"}


def _strip_location(tokens, truncated):
    """Drops a trailing "CITY PROVINCE" (or a cut-off piece of one) from a merchant line."""
    tokens = list(tokens)
    had_code = False
    while len(tokens) > 1:
        last = tokens[-1]
        if last in LOCATION_CODES:
            tokens.pop()
            had_code = True
            continue
        # A row cut at 32 characters can end on the first letter of a province ("CALGARY A").
        if truncated and len(last) == 1 and any(code.startswith(last) for code in LOCATION_CODES):
            tokens.pop()
            had_code = True
            continue
        break
    # Now the city, if the line ends with one (or the start of one, when cut off).
    for size in (3, 2, 1):
        if len(tokens) <= size:
            continue
        tail = " ".join(tokens[-size:])
        if tail in CITIES or (truncated and len(tail) >= 4 and any(
                city.startswith(tail) and city != tail and city.count(" ") == size - 1 for city in CITIES)):
            return tokens[:-size]
    if had_code and len(tokens) > 2:
        return tokens[:-1]  # an unknown town before a province code
    return tokens


def clean_merchant(text):
    """'SQ *CORNER BAKERY ROAS CALGARY' → 'Corner Bakery Roas'."""
    raw = " ".join(str(text).split())
    truncated = len(raw) >= 30
    upper = raw.upper()
    upper = _PROCESSOR.sub("", upper)
    upper = re.sub(r"\b(WWW\.|HTTPS?://)", "", upper)
    tokens = [t for t in re.split(r"\s+", upper) if t]
    tokens = _strip_location(tokens, truncated)
    tokens = [t for t in tokens if not _is_store_number(t) and not re.fullmatch(r"\(.*\)", t)]
    tokens = [t.rstrip("*") for t in tokens if t.rstrip("*")]
    name = " ".join(tokens).strip(" -*#")
    return title_case(name) if name else title_case(raw)


def _full_text(name, memo):
    """Chequing rows cut Name at 32 characters and repeat it in full in Memo."""
    name = " ".join(str(name or "").split())
    memo = " ".join(str(memo or "").split())
    if memo and len(memo) > len(name) and memo.upper().startswith(name.upper()[:24]):
        return memo
    return name


def describe(name, memo="", account_kind="chequing"):
    """What a bank row is and who it's with. See the module docstring."""
    text = _full_text(name, memo)
    upper = text.upper()
    is_card = account_kind in ("credit_card", "line_of_credit")

    m = re.match(r"INTERAC E-TRANSFER (?:FROM|TO):?\s*(.+)", upper)
    if m:
        person = title_case(text[m.start(1):].strip())
        return Description(person, person_key(person), "person")

    m = re.match(r"EFT (?:WITHDRAWAL TO|DEPOSIT FROM)\s+(.+)", upper)
    if m:
        org = text[m.start(1):].strip()
        brand = brand_of(org)
        if brand:
            return Description(brand, merchant_key(brand), "organization", brand=True)
        words = org.split()
        if len(words) > 1 and len(words[-1]) == 1:
            words = words[:-1]  # cut off at 32 characters: "ATB FInancial L"
        cleaned = " ".join(w.upper() if _looks_like_acronym(w) else title_case(w) for w in words)
        return Description(cleaned, merchant_key(cleaned), "organization")

    m = re.match(r"INTERAC - PURCHASE - (.+)", upper)
    if m:
        text = text[m.start(1):].strip()
        upper = text.upper()

    if re.search(r"CREDIT CARD PAYMENT|^PAYMENT - THANK YOU|^PAYMENT RECEIVED|^PAIEMENT", upper):
        return Description("Credit card payment", "creditcardpayment", "bank", hint="transfer")
    if re.search(r"\bLINE OF CREDIT\b|\bLOC (PAYMENT|ADVANCE|TRANSFER)\b", upper):
        return Description("Line of credit transfer", "lineofcredittransfer", "bank", hint="transfer")
    if upper == "INTEREST PAID" or (not is_card and upper.startswith("INTEREST")):
        return Description("Interest earned", "interestearned", "bank", hint="income")
    if is_card and "INTEREST" in upper:
        return Description("Interest charge", "interestcharge", "bank", hint="interest")
    if "MONEY-BACK REWARD" in upper or "CASH BACK REWARD" in upper or "CASHBACK REWARD" in upper:
        return Description("Money-Back Reward", "moneybackreward", "bank", hint="income")
    if "PROMO" in upper and is_card:
        return Description("Card promotion", "cardpromotion", "bank", hint="income")
    if re.search(r"\b(ABM|ATM)\b", upper):
        if upper.startswith("DEPOSIT"):
            return Description("Cash deposit", "cashdeposit", "bank", hint="cash")
        return Description("Cash withdrawal", "cashwithdrawal", "bank", hint="cash")
    if re.search(r"\bANNUAL FEE\b|\bSERVICE (FEE|CHARGE)\b|\bOVERLIMIT\b|\bNSF\b", upper):
        return Description("Bank fee", "bankfee", "bank", hint="fee")

    brand = brand_of(upper)
    if brand:
        return Description(brand, merchant_key(brand), "merchant", brand=True)
    cleaned = clean_merchant(text)
    return Description(cleaned, merchant_key(cleaned), "merchant")


def find_similar(name, key, known):
    """The best match for a new payee (`name`, `key`) among `known`
    ([(key, payee_id, payee_name)]).

    Returns (payee_id, payee_name, score, sure) or None. `sure` means it's safe to
    file under that payee without asking; otherwise it's worth a question.
    """
    from rapidfuzz import fuzz

    if not key or not known:
        return None
    is_person = key.startswith("person:")
    bare = key.split(":", 1)[1] if is_person else key
    best = None
    for other, payee_id, payee_name in known:
        if other.startswith("person:") != is_person or other == key:
            continue
        other_bare = other.split(":", 1)[1] if is_person else other
        if not other_bare:
            continue
        if is_person:
            # People: "Jordan A Sample" and "Jordan Sample" are the same person.
            score = fuzz.token_set_ratio(_spaced(name), _spaced(payee_name))
        else:
            score = fuzz.ratio(bare, other_bare)
            # One is the other cut short (Tangerine truncates at 32 characters).
            shorter, longer = sorted((bare, other_bare), key=len)
            if len(shorter) >= 8 and longer.startswith(shorter):
                score = max(score, 96)
        if best is None or score > best[2]:
            best = (payee_id, payee_name, score)
    if best is None:
        return None
    sure_at = 90 if is_person else (85 if len(bare) >= 6 else 92)
    ask_at = 80 if is_person else 75
    if best[2] >= sure_at:
        return (*best, True)
    if best[2] >= ask_at:
        return (*best, False)
    return None


def _spaced(name):
    return " ".join(re.sub(r"[^a-z0-9 ]", "", str(name).lower()).split())
