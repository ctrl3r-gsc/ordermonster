import asyncio
import os
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
        result = await parse_order_text(raw_text)
        print("\n✅ Успех! Результат парсинга от Gemini:")
        print(result)
    except Exception as e:
        print(f"\n❌ Произошла ошибка при вызове парсера: {e}")
        print("Проверь, правильно ли указан GEMINI_API_KEY в файле .env")

if __name__ == "__main__":
    asyncio.run(test())