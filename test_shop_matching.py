import asyncio
import os

from services.parser import OrderItem, parse_order_text, resolve_shop_from_order_text
from test_short_order_parsing import SHORT_ORDER_CATALOG


def parse(text: str, shops: list[str] | None = None) -> dict:
    os.environ["GEMINI_API_KEY"] = ""
    return asyncio.run(parse_order_text(text, existing_shops=shops, catalog_products=SHORT_ORDER_CATALOG))


def test_irrelevant_intro_text_should_not_become_shop() -> None:
    result = parse("Ok\nCan you send\n10 browni\n10 cookie too smyrna", ["Smyrna"])

    assert result["shop_name"] == "SMYRNA"
    assert [item["quantity"] for item in result["items"]] == [10, 10]
    assert {item["product_id"] for item in result["items"]} == {11, 12}


def test_long_irrelevant_sentence_before_order_is_ignored_as_shop() -> None:
    result = parse(
        "Customer was asking yesterday and said maybe we can do delivery today\n"
        "10 brownie\n"
        "10 cookie\n"
        "smyrna",
        ["Smyrna"],
    )

    assert result["shop_name"] == "SMYRNA"


def test_no_shop_present_does_not_create_fake_shop_from_text() -> None:
    result = parse("Customer was asking yesterday\n10 brownie\n10 cookie", ["Smyrna"])

    assert result["shop_name"] is None
    assert result["needs_shop_clarification"] is False


def test_unknown_trailing_text_does_not_become_shop() -> None:
    result = parse("10 brownie\n10 cookie\nrandomunknownshop", ["Smyrna"])

    assert result["shop_name"] is None


def test_destination_marker_matches_existing_shop() -> None:
    result = parse("10 brownie 10 cookie to smyrna", ["Smyrna"])

    assert result["shop_name"] == "SMYRNA"


def test_typo_destination_marker_matches_existing_shop() -> None:
    result = parse("10 brownie 10 cookie too smyrna", ["Smyrna"])

    assert result["shop_name"] == "SMYRNA"


def test_fuzzy_trailing_shop_matches_when_confident() -> None:
    result = parse("10 brownie 10 cookie smyrn", ["Smyrna"])

    assert result["shop_name"] == "SMYRNA"


def test_ambiguous_shops_need_clarification() -> None:
    result = parse("10 brownie smyrna", ["Smyrna", "Smyrna 2"])

    assert result["shop_name"] is None
    assert result["needs_shop_clarification"] is True
    assert set(result["shop_candidates"]) == {"Smyrna", "Smyrna 2"}


def test_gemini_wrong_shop_candidate_is_not_final_authority() -> None:
    items = [
        OrderItem(
            product_name="BROWNIE 100mg THC",
            raw_product_text="10 browni",
            original_text="10 browni",
            line_index=2,
            quantity=10,
        ),
        OrderItem(
            product_name="COOKIES 100mg THC",
            raw_product_text="10 cookie too smyrna",
            original_text="10 cookie too smyrna",
            line_index=3,
            quantity=10,
        ),
    ]

    resolution = resolve_shop_from_order_text(
        "Ok\nCan you send\n10 browni\n10 cookie too smyrna",
        items,
        ["Smyrna"],
        parser_shop_name="OK",
    )

    assert resolution.shop_name == "Smyrna"
    assert resolution.needs_clarification is False


def test_existing_short_shop_is_not_chosen_from_greeting() -> None:
    result = parse("Ok\nCan you send\n10 brownie\n10 cookie\nsmyrna", ["OK", "Smyrna"])

    assert result["shop_name"] == "SMYRNA"
