"""The Categories pop-up and the category pop-over: no two categories (or category groups)
with one name, dragging categories and groups, what a used category is used by, and the
recurring charges a category's pop-over lists."""

import ledger
from helpers import call, categories

CATS = {"Everyday": ["Food", "Gifts"], "Bills": ["Phone", "Power"]}


def refused(app, request, *args):
    result = app.dispatch(request, list(args))
    assert not result["ok"]
    return result["error"]


def groups(app):
    return {g["name"]: g for g in call(app, "categories")}


def names(app, group):
    return [c["name"] for c in groups(app)[group]["categories"]]


def with_income_and_cards(app):
    with app.db.write() as c:
        ledger.income_category(c)
        ledger.interest_category(c)
    return groups(app)


def test_no_two_categories_share_a_name(app):
    ids = categories(app, CATS)
    bills = groups(app)["Bills"]["id"]
    assert refused(app, "add_category", bills, "  gifts ") == "There's already a Gifts in Everyday."
    assert refused(app, "rename_category", ids["Food"], "GIFTS") == "There's already a Gifts in Everyday."
    assert call(app, "rename_category", ids["Gifts"], "gifts") is True  # its own name, written another way
    assert names(app, "Everyday") == ["Food", "gifts"]


def test_a_hidden_categorys_name_offers_it_back(app):
    ids = categories(app, CATS)
    bills = groups(app)["Bills"]["id"]
    call(app, "set_category_hidden", ids["Gifts"], True)
    assert refused(app, "add_category", bills, "Gifts") == "There's already a Gifts in Everyday (hidden)."

    assert call(app, "show_category", ids["Gifts"], bills) is True  # back where its name was typed, at the end
    g = groups(app)
    assert [(c["name"], c["hidden"]) for c in g["Bills"]["categories"]] == [("Phone", 0), ("Power", 0), ("Gifts", 0)]
    assert names(app, "Everyday") == ["Food"]
    assert call(app, "undo")["undone"] == "Show category"
    assert [(c["name"], c["hidden"]) for c in groups(app)["Everyday"]["categories"]] == [("Food", 0), ("Gifts", 1)]


def test_no_two_category_groups_share_a_name(app):
    categories(app, CATS)
    everyday = groups(app)["Everyday"]["id"]
    assert refused(app, "add_group", "bills") == "There's already a group called Bills."
    assert refused(app, "rename_group", everyday, "BILLS") == "There's already a group called Bills."
    assert call(app, "rename_group", everyday, "EVERYDAY") is True


def test_a_category_is_dragged_within_its_group_or_into_another(app):
    ids = categories(app, CATS)
    g = groups(app)
    call(app, "move_category", ids["Gifts"], g["Everyday"]["id"], ids["Food"])
    assert names(app, "Everyday") == ["Gifts", "Food"]
    call(app, "move_category", ids["Food"], g["Bills"]["id"], ids["Power"])
    assert names(app, "Everyday") == ["Gifts"]
    assert names(app, "Bills") == ["Phone", "Food", "Power"]
    call(app, "move_category", ids["Gifts"], g["Bills"]["id"])  # dropped at the end
    assert names(app, "Bills") == ["Phone", "Food", "Power", "Gifts"]
    assert call(app, "undo")["undone"] == "Move category"
    assert names(app, "Everyday") == ["Gifts"]


def test_categories_never_move_into_or_out_of_income_or_the_cards_group(app):
    ids = categories(app, CATS)
    g = with_income_and_cards(app)
    income, cards = g["Income"], next(x for x in g.values() if x["role"] == "debt")
    interest = next(c["id"] for c in cards["categories"])
    salary = call(app, "add_category", income["id"], "Side Work")

    assert refused(app, "move_category", ids["Food"], income["id"]) == "Categories can't be moved into Income."
    assert refused(app, "move_category", ids["Food"], cards["id"]) == f"Categories can't be moved into {cards['name']}."
    assert refused(app, "move_category", salary, g["Bills"]["id"]) == "The categories in Income stay where they are."
    # Not even within Income: the first category in it is the app's own Income.
    assert refused(app, "move_category", salary, income["id"], income["categories"][0]["id"]) \
        == "The categories in Income stay where they are."
    assert refused(app, "move_category", interest, g["Bills"]["id"]).startswith("The categories in ")
    assert names(app, "Income") == ["Income", "Side Work"]


def test_a_category_group_is_dragged_and_income_stays_last(app):
    categories(app, {**CATS, "Fun": ["Games"]})
    g = with_income_and_cards(app)
    order = lambda: [x["name"] for x in call(app, "categories")]
    cards = next(x["name"] for x in g.values() if x["role"] == "debt")

    call(app, "move_group", g["Fun"]["id"], g["Everyday"]["id"])
    assert order()[:3] == ["Fun", "Everyday", "Bills"]
    call(app, "move_group", g["Everyday"]["id"])  # dropped at the end: still above Income
    assert order()[-2:] == ["Everyday", "Income"]
    assert cards in order()
    assert call(app, "undo")["undone"] == "Move group"
    assert order()[:3] == ["Fun", "Everyday", "Bills"]
    assert refused(app, "move_group", g["Income"]["id"], g["Fun"]["id"]) == "This group stays where it is."


def test_what_a_used_category_is_used_by(app):
    ids = categories(app, CATS)
    with_income_and_cards(app)
    assert call(app, "category_uses", ids["Gifts"]) == {"transactions": 0, "assigned": False, "recurring": 0, "examples": 0}
    call(app, "set_assigned", "2026-10", ids["Gifts"], 5000)
    call(app, "add_subscription", payee="Sample Phone Co", name="Phone plan", amount=5500, kind="bill",
         category_id=ids["Phone"])
    assert call(app, "category_uses", ids["Gifts"])["assigned"] is True
    assert call(app, "category_uses", ids["Phone"])["recurring"] == 1
    own = {c["name"] for g in call(app, "categories") for c in g["categories"] if c["own"]}
    assert own == {"Income", ledger.INTEREST_CATEGORY_NAME}


def test_a_categorys_pop_over_lists_the_recurring_charges_whose_category_it_is(app):
    ids = categories(app, CATS)
    phone = call(app, "add_subscription", payee="Sample Phone Co", name="Phone plan", amount=5500, kind="bill",
                 category_id=ids["Food"])
    call(app, "add_subscription", payee="Sample Streaming", name="Sample Streaming", amount=12000, cadence="yearly",
         category_id=ids["Phone"])
    call(app, "add_subscription", payee="Sample Gym", name="Sample Gym", amount=3000, category_id=ids["Phone"])

    # Moved to Phone with nothing charged there yet: it's listed all the same.
    call(app, "update_subscription", phone, {"category_id": ids["Phone"]})
    recurring = call(app, "category_detail", ids["Phone"])["recurring"]
    assert [s["name"] for s in recurring["bills"]] == ["Phone plan"]
    assert [s["name"] for s in recurring["subscriptions"]] == ["Sample Gym", "Sample Streaming"]
    assert recurring["monthly"] == 5500 + 3000 + 1000  # a yearly one counts a twelfth a month
    assert call(app, "category_detail", ids["Food"])["recurring"] == {"subscriptions": [], "bills": [], "monthly": 0}

    gym = next(s["id"] for s in recurring["subscriptions"] if s["name"] == "Sample Gym")
    call(app, "cancel_subscription", gym)
    recurring = call(app, "category_detail", ids["Phone"])["recurring"]
    assert [s["name"] for s in recurring["subscriptions"]] == ["Sample Streaming"]
    assert recurring["monthly"] == 6500


def test_a_group_is_added_above_the_one_whose_plus_was_clicked_with_a_category_to_rename(app):
    categories(app, CATS)
    with_income_and_cards(app)
    g = groups(app)
    order = lambda: [x["name"] for x in call(app, "categories")]
    fun = call(app, "add_group", "Fun", g["Bills"]["id"], True)
    assert order()[:3] == ["Everyday", "Fun", "Bills"]
    assert names(app, "Fun") == ["New Spending Category"]
    call(app, "add_group", "Trips", g["Everyday"]["id"], True)
    assert order()[0] == "Trips"
    assert names(app, "Trips") == ["New Spending Category 2"]  # the first one's name is taken
    call(app, "add_group", "Last", g["Income"]["id"], True)  # before Income: the bottom
    assert order()[-2:] == ["Last", "Income"]
    assert call(app, "undo")["undone"] == "Add group Last"  # the group and its category, one step
    assert "Last" not in order() and "New Spending Category 3" not in names(app, "Fun") + names(app, "Trips")
    assert call(app, "add_group", "Plain") and names(app, "Plain") == []  # onboarding's own list: its choice
    assert fun


def test_a_category_is_added_above_the_one_whose_plus_was_clicked(app):
    ids = categories(app, CATS)
    g = with_income_and_cards(app)
    call(app, "add_category", g["Bills"]["id"], "Internet", ids["Power"])
    assert names(app, "Bills") == ["Phone", "Internet", "Power"]
    call(app, "add_category", g["Bills"]["id"], "Water")
    assert names(app, "Bills") == ["Phone", "Internet", "Power", "Water"]
    # Never above the app's own Income, the first in its group.
    call(app, "add_category", g["Income"]["id"], "Side Work", g["Income"]["categories"][0]["id"])
    assert names(app, "Income") == ["Income", "Side Work"]
    assert call(app, "category_detail", ids["Phone"])["hidden"] == 0
