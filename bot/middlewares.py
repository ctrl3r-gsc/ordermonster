from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from db import SessionLocal


class AllowedUsersMiddleware(BaseMiddleware):
    def __init__(self, allowed_users: set[int], allowed_chats: set[int]) -> None:
        self.allowed_users = allowed_users
        self.allowed_chats = allowed_chats

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat

        user_allowed = user is not None and user.id in self.allowed_users
        chat_allowed = chat is not None and chat.id in self.allowed_chats
        whitelist_enabled = bool(self.allowed_users or self.allowed_chats)

        if whitelist_enabled and not (user_allowed or chat_allowed):
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None
        return await handler(event, data)


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with SessionLocal() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
