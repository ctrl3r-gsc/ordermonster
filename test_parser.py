import asyncio
import json
import os
from pathlib import Path
from dotenv import load_dotenv
# Убедись, что папка называется services и файл внутри неё parser.py
from services.parser import parse_order_text  

# Загружаем переменные из файла .env
load_dotenv()

async def test():
    # Тестовый хаотичный текст (можешь поменять на любой другой для проверки)
    raw_text = "бро привет, запиши нам 10 пачек гамми 500мг клубника в шаман, оплата налик"
    
    print("--- Запуск теста парсера Gemini ---")
    print(f"Входной текст: '{raw_text}'\n")
    print("Отправка запроса в Google AI Studio...")
    
    try:
        raw_catalog = json.loads(Path("data/current_products.json").read_text(encoding="utf-8"))
        catalog = [
            {
                "product_id": index,
                "name": item["name"],
                "dosage": item.get("dosage"),
                "price": item.get("price"),
                "aliases": item.get("aliases", []),
            }
            for index, item in enumerate(raw_catalog, start=1)
        ]
        result = await parse_order_text(raw_text, catalog_products=catalog)
        print("\n✅ Успех! Результат парсинга от Gemini:")
        print(result)
    except Exception as e:
        print(f"\n❌ Произошла ошибка при вызове парсера: {e}")
        print("Проверь, правильно ли указан GEMINI_API_KEY в файле .env")

if __name__ == "__main__":
    asyncio.run(test())
