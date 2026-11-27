from types import SimpleNamespace

from db.models import DeliveryStatus
from handlers.dashboard import order_state_emoji
from handlers.orders import dashboard_order_state_emoji


def make_order(delivery_status: DeliveryStatus, payment_status: str) -> SimpleNamespace:
    return SimpleNamespace(delivery_status=delivery_status, payment_status=SimpleNamespace(value=payment_status))


def test_legacy_dashboard_uses_the_same_status_icons() -> None:
    for delivery_status, payment_status, expected in (
        (DeliveryStatus.pending_shipment, "unpaid", "⌛"),
        (DeliveryStatus.shipped, "paid", "🚚"),
        (DeliveryStatus.delivered, "partially_paid", "📦"),
        (DeliveryStatus.delivered, "paid", "✅"),
    ):
        assert dashboard_order_state_emoji(make_order(delivery_status, payment_status)) == expected


def test_dashboard_order_state_emoji_is_hourglass_before_shipment() -> None:
    assert order_state_emoji(make_order(DeliveryStatus.pending_shipment, "unpaid")) == "⏳"


def test_dashboard_order_state_emoji_is_truck_while_shipped() -> None:
    assert order_state_emoji(make_order(DeliveryStatus.shipped, "partially_paid")) == "🚚"
    assert order_state_emoji(make_order(DeliveryStatus.shipped, "paid")) == "🚚"


def test_dashboard_order_state_emoji_is_box_after_delivery_until_fully_paid() -> None:
    assert order_state_emoji(make_order(DeliveryStatus.delivered, "unpaid")) == "📦"
    assert order_state_emoji(make_order(DeliveryStatus.delivered, "partially_paid")) == "📦"


def test_dashboard_order_state_emoji_is_checkmark_only_after_delivery_and_full_payment() -> None:
    assert order_state_emoji(make_order(DeliveryStatus.delivered, "paid")) == "✅"
