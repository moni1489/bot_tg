from __future__ import annotations

from funko_deal_bot.models import Listing

ZIP = "19801"


def _item(
    item_id: str,
    title: str,
    price: float,
    shipping: float,
) -> Listing:
    label = "Free" if shipping == 0 else f"${shipping:.2f}"
    return Listing(
        item_id,
        title,
        f"https://www.ebay.com/itm/{item_id}",
        price,
        shipping_cost=shipping,
        shipping_label=label,
        ship_to_zip=ZIP,
        shipping_quoted=True,
    )


SEED_LISTINGS: list[Listing] = [
    _item("1001", "Funko Pop Marvel Spider-Man #03 Vinyl Figure", 28.99, 0),
    _item("1002", "FUNKO POP! Marvel: Spider-Man 03 NIB", 27.50, 4.99),
    _item("1003", "Spider-Man Funko Pop 3 Marvel Comics", 29.95, 0),
    _item("1004", "Funko Pop Marvel Spider-Man 03 Hot Topic Exclusive", 31.00, 5.50),
    _item("1005", "Funko Pop Star Wars Darth Vader #01", 16.40, 4.25),
    _item("1006", "Darth Vader Funko Pop Star Wars 01 vinyl", 15.99, 0),
    _item("1007", "Funko Pop Star Wars Darth Vader 1", 18.25, 3.99),
    _item("1008", "Funko Pop Disney Mickey Mouse #01", 14.00, 0),
    _item("1009", "Mickey Mouse Funko Pop Disney 01", 13.50, 4.00),
    _item("1010", "Funko Pop Stranger Things Eleven #421", 22.00, 5.00),
    _item("1011", "Eleven Funko Pop Stranger Things 421", 24.50, 0),
    _item("1012", "Funko Pop Stranger Things Eleven #421 NIB", 21.80, 4.50),
    _item("1013", "Funko Pop Batman #01 DC Comics", 12.99, 0),
    _item("1014", "Batman Funko Pop 01 vinyl figure", 13.40, 3.50),
    _item("1015", "Funko Pop Batman 1 DC", 14.20, 0),
    _item("1016", "Funko Pop Marvel Deadpool #20", 40.00, 0),
    _item("1017", "Deadpool Funko Pop #20 vinyl", 42.00, 0),
    _item("1018", "Funko Pop Deadpool 20", 41.00, 0),
    _item("1019", "Funko Pop Marvel Venom #82", 35.00, 0),
    _item("1020", "Venom Funko Pop #82", 36.00, 0),
    _item("1021", "Funko Pop Venom 82 vinyl", 34.00, 0),
    _item("1022", "Funko Pop Movies Gladiator Maximus #860", 32.00, 6.00),
    _item("1023", "Maximus Funko Pop #860 Gladiator", 34.00, 5.50),
    _item("1024", "Funko Pop Maximus 860 vinyl", 31.00, 7.00),
    _item("1025", "Funko Pop TV Tony Soprano #1295", 48.00, 0),
    _item("1026", "Tony Soprano Funko Pop 1295 The Sopranos", 50.00, 0),
    _item("1027", "Funko Pop Silvio Dante The Sopranos", 36.00, 0),
    _item("1028", "Silvio Dante Funko Pop vinyl", 38.00, 0),
    _item("1029", "Funko Pop Christopher Moltisanti The Sopranos", 33.00, 0),
    _item("1030", "Christopher Funko Pop The Sopranos", 35.00, 0),
]

NEW_DEAL = _item("2001", "Funko Pop Marvel Spider-Man #03 New", 12.99, 3.99)
NEW_NORMAL = _item("2002", "Funko Pop Star Wars Darth Vader #01", 16.10, 4.00)
NEW_BUNDLE = _item("2003", "Funko Pop Marvel lot of 3 Spider-Man Deadpool Venom", 49.00, 12.00)
NEW_AUCTION = _item("2004", "Funko Pop Stranger Things Eleven #421 auction bid", 0.99, 5.00)
NEW_FAKE_CHEAP = _item("2005", "Funko Pop Marvel Spider-Man #03", 9.99, 18.00)
NEW_HALF_OFF = _item("2006", "Funko Pop Marvel Spider-Man #03", 8.00, 0.0)
NEW_MAXIMUS = _item("2007", "Funko Pop Movies Gladiator Maximus #860", 17.00, 6.72)
NEW_SOPRANOS = _item(
    "2008",
    "Funko Pop lot of 3 Tony Soprano 1295, Silvio Dante, Christopher",
    70.00,
    9.00,
)


def demo_catalog() -> list[Listing]:
    return list(SEED_LISTINGS)


def demo_new_listings() -> list[Listing]:
    return [
        NEW_DEAL,
        NEW_NORMAL,
        NEW_BUNDLE,
        NEW_AUCTION,
        NEW_FAKE_CHEAP,
        NEW_HALF_OFF,
        NEW_MAXIMUS,
        NEW_SOPRANOS,
    ]
