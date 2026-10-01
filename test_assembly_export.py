import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from aiohttp import web
from assembly_api import create_app, orders


class ExportTests(IsolatedAsyncioTestCase):
    async def test_unauthorized_never_queries_database(self):
        request = SimpleNamespace(app={"export_token": "x" * 64}, headers={})
        with patch("assembly_api.SessionLocal") as factory:
            with self.assertRaises(web.HTTPUnauthorized):
                await orders(request)
            factory.assert_not_called()

    async def test_empty_snapshot_and_limit(self):
        request = SimpleNamespace(app={"export_token": "x" * 64}, headers={"Authorization": "Bearer " + "x" * 64})
        for records, expected_status in (([], 200), ([None] * 10001, 503)):
            session = AsyncMock()
            result = Mock()
            result.all.return_value = records
            session.scalars.return_value = result
            context = AsyncMock()
            context.__aenter__.return_value = session
            with patch("assembly_api.SessionLocal", return_value=context):
                if expected_status == 503:
                    with self.assertRaises(web.HTTPServiceUnavailable):
                        await orders(request)
                else:
                    response = await orders(request)
                    from sqlalchemy.dialects import postgresql
                    query = str(session.scalars.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
                    self.assertIn("orders.delivery_status = 'pending_shipment'", query)
                    self.assertEqual(json.loads(response.text)["orders"], [])
                    self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_secret_fails_closed(self):
        with self.assertRaises(RuntimeError):
            create_app("")
