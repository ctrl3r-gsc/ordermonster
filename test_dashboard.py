from types import SimpleNamespace

from db.models import DeliveryStatus
from handlers.dashboard import order_state_emoji


def make_order(delivery_status: DeliveryStatus, payment_status: str) -> SimpleNamespace:
    return SimpleNamespace(delivery_status=delivery_status, payment_status=SimpleNamespace(value=payment_status))


def test_dashboard_order_state_emoji_is_hourglass_before_shipment() -> None:
    assert order_state_emoji(make_order(DeliveryStatus.pending_shipment, "unpaid")) == "⏳"


def test_dashboard_order_state_emoji_is_truck_after_shipment_or_delivery_when_unpaid() -> None:
    assert order_state_emoji(make_order(DeliveryStatus.shipped, "partially_paid")) == "🚚"
    assert order_state_emoji(make_order(DeliveryStatus.delivered, "unpaid")) == "🚚"


def test_dashboard_order_state_emoji_is_checkmark_after_shipment_or_delivery_when_paid() -> None:
    assert order_state_emoji(make_order(DeliveryStatus.shipped, "paid")) == "✅"
    assert order_state_emoji(make_order(DeliveryStatus.delivered, "paid")) == "✅"
