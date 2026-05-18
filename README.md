# Orderbot

Асинхронный Telegram-бот для CRM и трекинга заказов: принимает сырой текст заказа, разбирает его через Gemini structured output, ведет оплату частями и управляет доставкой через inline-кнопки.

## Что реализовано

- Telegram bot на Aiogram 3.
- PostgreSQL и SQLAlchemy 2.0 async ORM.
- Gemini structured output parser с fallback-парсером без API.
- Доступ только для пользователей из `ALLOWED_USERS`.
- Интерактивная карточка заказа через inline keyboard.
- Полная оплата и split payments с расчетом `Paid / Remaining`.
- Статусы доставки: `pending_shipment`, `shipped`, `delivered`.
- `/dashboard` для активных или проблемных заказов.
- Миграция Telegram `result.json` с автосидингом магазинов, заказов и товаров.
- Актуальный прайс хранится в `data/current_products.json` и сидится перед историческими товарами.
- При старте бот автоматически обновляет товары из `data/current_products.json`.

## Запуск через Docker Compose

1. Создай `.env` из примера:

```bash
cp .env.example .env
```

2. Заполни `.env`:

```env
BOT_TOKEN=your_telegram_bot_token
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-1.5-flash
ALLOWED_USERS=123456789,987654321

POSTGRES_DB=orderbot
POSTGRES_USER=orderbot
POSTGRES_PASSWORD=change_me
POSTGRES_HOST=db
POSTGRES_PORT=5432
```

3. Запусти проект:

```bash
docker compose up --build
```

4. Один раз запусти миграцию истории:

```bash
docker compose run --rm bot python -m migration.migrate --file result.json
```

Актуальный каталог по умолчанию берется из `data/current_products.json`. Можно передать другой файл:

```bash
docker compose run --rm bot python -m migration.migrate --file result.json --catalog data/current_products.json
```

## Использование бота

- Отправь или перешли сырой текст заказа.
- Если магазин распознан, заказ создается сразу.
- Если магазин не распознан, бот покажет Top-10 магазинов и кнопку создания нового.
- В карточке заказа доступны `Edit Payment` и `Edit Delivery`.
- `Paid in Full` списывает весь остаток после выбора метода оплаты.
- `Split Payment` просит сумму, затем метод, после чего обновляет баланс.
- `Shipped` просит трек-номер.
- `Delivered` закрывает карточку как доставленную.
- `/dashboard` показывает заказы, где доставка не `delivered` или оплата не `paid`.

## Локальная разработка

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Запусти PostgreSQL:

```bash
docker compose up db
```

Для локального запуска бота поставь `POSTGRES_HOST=localhost` в `.env` и выполни:

```bash
python main.py
```

Миграция локально:

```bash
python -m migration.migrate --file result.json
```

