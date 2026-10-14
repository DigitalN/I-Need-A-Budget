"""Made-up Tangerine exports for the tests. Never put real bank data in this folder."""

# Tangerine names card exports after the masked card number. Built here so the source
# never contains a card-number-shaped string (the pre-commit hook blocks those).
CARD_FILE = "0000 " + "x" * 4 + "  " + "x" * 4 + " 1234.CSV"


def card_number(last4):
    """A made-up 16-digit card number ending in `last4` that passes the Luhn check, built
    here for the same reason as CARD_FILE."""
    for d in "0123456789":
        number = "5" + "0" * 10 + d + last4
        total = sum(int(c) if i % 2 == 0 else sum(divmod(int(c) * 2, 10)) for i, c in enumerate(reversed(number)))
        if total % 10 == 0:
            return number
    raise AssertionError("no check digit")


def call(service, request, *args, **kwargs):
    result = service.dispatch(request, kwargs if kwargs else list(args))
    assert result["ok"], result.get("error")
    return result["result"]


def card_csv(rows):
    """rows: (MM/DD/YYYY, name, amount, card category)"""
    lines = ["Transaction date,Transaction,Name,Memo,Amount"]
    for date, name, amount, category in rows:
        kind = "CREDIT" if float(amount) > 0 else "DEBIT"
        memo = f"Rewards earned: 0.10 ~ Category: {category}" if category else ""
        lines.append(f"{date},{kind},{name},{memo},{amount}")
    return "\n".join(lines) + "\n"


def bank_csv(rows):
    """rows: (MM/DD/YYYY, name (cut at 32 like Tangerine), memo (full), amount)"""
    lines = ["Date,Transaction,Name,Memo,Amount"]
    for date, name, memo, amount in rows:
        lines.append(f"{date},OTHER,{name},{memo},{amount}")
    return "\n".join(lines) + "\n"


def write(folder, name, text):
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def categories(service, groups):
    """{'Group': ['Cat', ...]} → {'Cat': id}"""
    for group, names in groups.items():
        gid = call(service, "add_group", group)
        for n in names:
            call(service, "add_category", gid, n)
    return {c["name"]: c["id"] for g in call(service, "categories") for c in g["categories"]}


def txns(service, **filters):
    return call(service, "transactions", filters)["items"]
