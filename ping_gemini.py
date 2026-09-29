import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))

print("Відправляємо тестовий запит до Gemini...")
try:
    response = client.models.generate_content(
        model='gemini-3.5-flash-lite', 
        contents='Ping. Відповідай лише словом Pong.'
    )
    print(f"✅ API працює! Відповідь серверів: {response.text}")
except Exception as e:
    print(f"❌ Сервери все ще перевантажені або недоступні.\nДеталі: {e}")