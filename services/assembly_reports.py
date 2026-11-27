import asyncio
from datetime import date, datetime, time, timedelta, timezone
from html import escape
import json
import logging
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select

from config import get_settings
from db.models import AssemblyDailyReport
from db.session import SessionLocal

logger = logging.getLogger(__name__)
BANGKOK = ZoneInfo("Asia/Bangkok")


def fetch_report(report_date: date) -> dict:
    settings = get_settings()
    url = settings.assembly_api_url.rstrip("/") + "/internal/daily-report?report_date=" + report_date.isoformat()
    request = Request(url, headers={"Authorization": "Bearer " + settings.assembly_api_token})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read(512 * 1024))


def format_report(report: dict) -> str:
    quality = report["quality_percent"]
    required = report["required"]
    completed = report["completed"]
    remaining = report["remaining"]
    if quality == 100:
        headline = "Все обязательные заказы закрыты вовремя. Отличная работа команды 👏"
    elif quality >= 90:
        headline = "Почти все заказы закрыты. Проверьте оставшиеся пункты."
    else:
        headline = "Есть заказы, которые требуют внимания сегодня."
    lines = [f"<b>Отчёт за {escape(report['report_date'])}</b>", "",
             f"Качество закрытия: <b>{quality}%</b>",
             f"Обязательных заказов: {required}", f"Доставлено и отмечено: {completed}",
             f"Осталось: {len(remaining)}", "", escape(headline)]
    if report["bonus_green"]:
        lines.append(f"Зелёных заказов закрыто дополнительно: {report['bonus_green']}")
    if remaining:
        lines.extend(["", "<b>Требуют внимания:</b>"])
        for item in remaining[:20]:
            lines.append(f"— #{escape(str(item['order_number']))} · {item['age_days']}-й день")
    return "\n".join(lines)


async def publish_daily_report(bot: Bot, report_date: date) -> bool:
    settings = get_settings()
    if not settings.assembly_api_url or not settings.assembly_api_token:
        return False
    report_key = report_date.isoformat()
    async with SessionLocal() as session:
        if await session.get(AssemblyDailyReport, report_key):
            return False
    report = await asyncio.to_thread(fetch_report, report_date)
    targets = settings.report_chat_ids or settings.allowed_chats
    if not targets:
        logger.warning("Daily report has no REPORT_CHAT_IDS or ALLOWED_CHATS target")
        return False
    message = format_report(report)
    for chat_id in targets:
        await bot.send_message(chat_id, message, parse_mode="HTML")
    async with SessionLocal.begin() as session:
        session.add(AssemblyDailyReport(report_date=report_key))
    return True


async def report_loop(bot: Bot) -> None:
    while True:
        now = datetime.now(BANGKOK)
        target = datetime.combine(now.date(), time(10, 0), BANGKOK)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep(max(1, (target - now).total_seconds()))
        try:
            await publish_daily_report(bot, target.date() - timedelta(days=1))
        except Exception as error:
            logger.warning("Daily Assembly report failed (%s)", type(error).__name__)
