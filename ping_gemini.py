"""Ручна перевірка: чи відповідає конкретна модель Gemini (реальний запит).

Запуск: python ping_gemini.py [назва-моделі]   (за замовчуванням — gemini-3.5-flash-lite)
"""
import os
import sys

from dotenv import load_dotenv
from google import genai

load_dotenv()
model = sys.argv[1] if len(sys.argv) > 1 else "gemini-3.5-flash-lite"

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

print(f"Відправляємо тестовий запит до {model}...")
try:
    response = client.models.generate_content(
        model=model,
        contents="Ping. Відповідай лише словом Pong.",
    )
    print(f"✅ API працює! Відповідь серверів: {response.text}")
except Exception as e:
    print(f"❌ Сервери все ще перевантажені або недоступні.\nДеталі: {e}")
