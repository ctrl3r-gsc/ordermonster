import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiogram.enums import ChatType

spec = importlib.util.spec_from_file_location("assembly_button", Path(__file__).parent / "handlers" / "assembly.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AssemblyButtonTests(IsolatedAsyncioTestCase):
    async def test_private_chat_receives_web_app_button(self):
        message = SimpleNamespace(chat=SimpleNamespace(type=ChatType.PRIVATE), answer=AsyncMock())
        await module.open_assembly(message)
        button = message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.web_app.url, "https://assembly.gosuay.com")
        self.assertIsNone(button.url)
        message.answer.assert_awaited_once()

    async def test_groups_do_not_receive_unsupported_web_app_button(self):
        for chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
            message = SimpleNamespace(chat=SimpleNamespace(type=chat_type), answer=AsyncMock())
            await module.open_assembly(message)
            self.assertNotIn("reply_markup", message.answer.call_args.kwargs)
            self.assertIn("/open_assembly", message.answer.call_args.args[0])
