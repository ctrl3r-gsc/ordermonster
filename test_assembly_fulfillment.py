import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase, IsolatedAsyncioTestCase, skipUnless
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool
from config import get_settings
from db.models import Base, DeliveryStatus, Order, PaymentStatus, Shop, AssemblyShipmentCommand
from services.fulfillment import ShipmentCommand, ShipmentConflict, apply_command, record_shipment


class AttributionTests(TestCase):
    def test_empty_track_does_not_erase_existing_and_retry_preserves_actor(self):
        order = SimpleNamespace(delivery_status=DeliveryStatus.pending_shipment, tracking_number="EXISTING",
                                shipped_by_telegram_id=None, shipped_by_name=None, shipped_at=None, shipment_source=None)
        record_shipment(order, 101, "First")
        time = order.shipped_at
        record_shipment(order, 202, "Second", "")
        self.assertEqual(order.tracking_number, "EXISTING")
        self.assertEqual(order.shipped_by_telegram_id, 101)
        self.assertEqual(order.shipped_at, time)

    def test_delivered_is_not_downgraded(self):
        order = SimpleNamespace(delivery_status=DeliveryStatus.delivered, tracking_number=None, shipped_by_telegram_id=None)
        record_shipment(order, 101, "Test", "TRACK")
        self.assertEqual(order.delivery_status, DeliveryStatus.delivered)
        self.assertIsNone(order.shipped_by_telegram_id)


@skipUnless(os.getenv("WORKFLOW_TEST") == "1", "Requires disposable database")
class CommandTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings = get_settings()
        if not settings.postgres_db.endswith("_test"):
            raise RuntimeError("Refusing production database")
        self.engine = create_async_engine(settings.database_url, poolclass=NullPool)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("TRUNCATE orders, shops, assembly_shipment_commands RESTART IDENTITY CASCADE"))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions.begin() as session:
            shop = Shop(name="Workflow test")
            session.add(shop)
            await session.flush()
            session.add(Order(id=1, shop_id=shop.id, user_id=101, payment_status=PaymentStatus.paid, delivery_status=DeliveryStatus.pending_shipment))

    async def asyncTearDown(self):
        await self.engine.dispose()

    def command(self, **changes):
        values = dict(request_id=uuid4(), order_id=1, action="ship", actor_id=101, actor_name="A", occurred_at=datetime.now(timezone.utc), tracking_number="TRACK")
        values.update(changes)
        return ShipmentCommand(**values)

    async def test_idempotent_shipping_payment_unchanged(self):
        command = self.command()
        async with self.sessions.begin() as session:
            result = await apply_command(session, command)
        async with self.sessions.begin() as session:
            repeated = await apply_command(session, command)
            self.assertEqual(repeated, result)
            order = await session.get(Order, 1)
            self.assertEqual(order.payment_status, PaymentStatus.paid)
            self.assertEqual(order.shipped_by_telegram_id, 101)
            self.assertEqual(order.tracking_number, "TRACK")

    async def test_duplicate_id_different_payload_rejected(self):
        command = self.command()
        async with self.sessions.begin() as session:
            await apply_command(session, command)
        async with self.sessions() as session:
            with self.assertRaises(ShipmentConflict):
                await apply_command(session, command.model_copy(update={"actor_id": 202}))
            await session.rollback()

    async def test_existing_external_actor_preserved(self):
        async with self.sessions.begin() as session:
            order = await session.get(Order, 1)
            record_shipment(order, 202, "Bot operator", "OLD")
        async with self.sessions.begin() as session:
            result = await apply_command(session, self.command())
            self.assertEqual(result["shipped_by_telegram_id"], 202)
            self.assertEqual(result["tracking_number"], "OLD")
            self.assertIn("note", result)

    async def test_tracking_conflict_and_success_keep_payment_and_actor(self):
        async with self.sessions.begin() as session:
            await apply_command(session, self.command())
        async with self.sessions() as session:
            with self.assertRaises(ShipmentConflict):
                await apply_command(session, self.command(action="tracking", expected_tracking_number="OUTDATED"))
            await session.rollback()
        async with self.sessions.begin() as session:
            result = await apply_command(session, self.command(action="tracking", expected_tracking_number="TRACK", tracking_number="NEW", actor_id=202))
            self.assertEqual(result["tracking_number"], "NEW")
            self.assertEqual(result["shipped_by_telegram_id"], 101)
            self.assertEqual((await session.get(Order, 1)).payment_status, PaymentStatus.paid)

    async def test_without_tracking_and_missing_order(self):
        async with self.sessions.begin() as session:
            result = await apply_command(session, self.command(tracking_number=None))
            self.assertIsNone(result["tracking_number"])
        async with self.sessions() as session:
            with self.assertRaises(LookupError):
                await apply_command(session, self.command(order_id=999))
            await session.rollback()

    async def test_delivered_and_reopen_round_trip(self):
        async with self.sessions.begin() as session:
            await apply_command(session, self.command())
        async with self.sessions.begin() as session:
            delivered = await apply_command(session, self.command(action="delivered", tracking_number=None))
            self.assertEqual(delivered["delivery_status"], "delivered")
        async with self.sessions.begin() as session:
            with self.assertRaises(ShipmentConflict):
                await apply_command(session, self.command(action="reopen"))
        # A reopened order is tested from the shipped state in a fresh fixture.
        async with self.sessions.begin() as session:
            order = await session.get(Order, 1)
            order.delivery_status = DeliveryStatus.shipped
            order.tracking_number = "TRACK"
            order.shipped_by_telegram_id = 101
            order.shipped_by_name = "A"
            reopened = await apply_command(session, self.command(action="reopen"))
            self.assertEqual(reopened["delivery_status"], "pending_shipment")
            self.assertIsNone(reopened["tracking_number"])
