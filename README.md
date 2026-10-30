# 🇭🇷 Croatia News Bot

Telegram-бот, який щогодини вдень збирає новини хорватських медіа (з фокусом на Задар) з RSS, відсіює зайве й дублікати за допомогою Gemini, перекладає відібране українською та публікує в Telegram-канал для української діаспори в Хорватії.

Репозиторій: <https://github.com/sergeygavrilyk/croatia-news-bot> (публічний, гілка `main`). Бот-близнюк: [Moldova-news-bot](https://github.com/sergeygavrilyk/Moldova-news-bot).

---

## Як це працює (коротко)

```
cron-job.org ──(HTTP POST, щогодини вдень)──▶ GitHub Actions: news_bot_run.yml
                                                │
                                                ├─ python news_bot.py
                                                │    1. scrape_feeds()    → нові статті в news_backlog.json
                                                │       (вік ≤ 12 год, стоп-слова, спорт/реклама, повний текст зі сторінки)
   RSS: 023.hr · Slobodna Dalmacija ·           │    2. process_backlog() → до 25 кандидатів → Gemini-редактор (JSON)
        Zadarski list ─────────────────────────▶│       → до 3 новин → переклад українською → перевірка → Telegram
   Google Gemini (AI Studio) ◀──────────────────│    3. finally: analytics.json, звіт адміну в Telegram
   Telegram Bot API ◀───────────────────────────│
                                                └─ git commit + push файлів стану
                                                   (posted_news.txt, posted_titles.txt, news_backlog.json, analytics.json)
```

Детально — у [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Стек
Python 3.11 · `feedparser` · `cloudscraper` + `beautifulsoup4` (повний текст статей) · `google-genai` (Gemini, структуровані JSON-відповіді через `pydantic`) · `tenacity` (повтори) · `requests` (Telegram Bot API) · GitHub Actions · cron-job.org.

## Структура папки

| Файл | Що це |
|---|---|
| `news_bot.py` | Увесь бот |
| `feeds.json` | RSS-джерела (`name`, `url`, `category`) |
| `posted_news.txt`, `posted_titles.txt` | Історія опублікованого (URL і заголовки, до 1000 рядків) — **пише бот** |
| `news_backlog.json` | Черга новин — **пише бот** |
| `analytics.json` | Статистика 100 останніх запусків — **пише бот** |
| `requirements.txt` | Залежності |
| `.github/workflows/news_bot_run.yml` | Запуск у хмарі + коміт стану |
| `check_models.py`, `ping_gemini.py`, `test_rss.py` | Ручні утиліти |
| `CLAUDE.md`, `project_state.md`, `docs/` | Документація й правила роботи (див. нижче) |
| `.env.example` | Шаблон змінних середовища |

## Змінні середовища

| Змінна | Обов'язкова | Що це |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | так | Токен бота від @BotFather |
| `TELEGRAM_CHANNEL_ID` | так | Канал для публікацій |
| `GEMINI_API_KEY` | так | Ключ Google AI Studio |
| `ADMIN_CHAT_ID` | ні | Чат для звітів і алертів |

Локально — у файлі `.env` (шаблон `.env.example`). У GitHub — Settings → Environments → `production` (для `main`) і `staging` (для інших гілок).

## Робота з локальною папкою (Windows)

```powershell
cd "$env:USERPROFILE\Desktop\PythonProjects"
git pull                      # бот комітить стан щогодини — завжди спершу pull
python -m venv .venv; .\.venv\Scripts\activate
pip install -r requirements.txt
python check_models.py        # які моделі доступні для ключа
python test_rss.py            # чи відповідають RSS-адреси
```

⚠️ `python news_bot.py` локально з робочим `.env` **публікує в справжній канал** і змінює файли стану — а наступний запуск у хмарі їх перезапише своїми. Для перевірок — тестовий режим: `$env:DRY_RUN=1; python news_bot.py` (див. «Типові операції»).

## Щоденний контроль (2 хвилини)
1. Адмін-чат: після кожного запуску з публікаціями приходить «📊 Звіт Croatia News Bot»; `⚠️ ALARM` — щось пішло не так.
2. GitHub → Actions: запуски зелені; після кожного є коміт «Update news history and backlog state».
3. Якщо запуск червоний — відкрити лог кроку «Run News Bot» і записати причину в `project_state.md` (§7).

## Типові операції
- **Додати/прибрати джерело** — змінити `feeds.json` (рішення власника). Перевірити адресу: `python test_rss.py` (оновіть список у скрипті) або відкрити RSS у браузері.
- **Змінити модель Gemini** — список `GEMINI_MODELS` у `news_bot.py` (р. 70) і мітки в `send_analytics_to_admin()`; спершу `python check_models.py`.
- **Замінити секрет** — @BotFather / AI Studio → GitHub Environments → локальний `.env`.
- **Зупинити бота** — вимкнути задачу в cron-job.org (workflow сам не стартує).
- **Тестовий запуск (`DRY_RUN`)** — перевірити зміну (промпт, модель, джерело) без публікації в канал і без зміни файлів стану: GitHub → **Actions → Croatia News Automator → Run workflow** → обрати гілку (зазвичай гілку PR) → поставити галочку **«Тестовий запуск»** → Run. В адмін-чат прийдуть до 3 готових постів з позначкою «🧪 ТЕСТ» і звіт «ТЕСТОВИЙ ЗАПУСК» (з причинами відхилень). RSS, фільтр і переклад — справжні, тож витрачається 1 запит до моделі фільтра і 1 до моделі перекладу; не запускати десятки разів на день. Локально: `$env:DRY_RUN=1; python news_bot.py` (потрібен `ADMIN_CHAT_ID` у `.env`).

## Документація і правила
- [CLAUDE.md](CLAUDE.md) — короткий контекст для AI-асистента.
- [project_state.md](project_state.md) — **живий журнал**: стан, питання, задачі, тех. борг, ідеї, рішення, журнал змін.
- [docs/AI_RULES.md](docs/AI_RULES.md) — правила роботи AI-агента (що можна самому, що — лише з погодження).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — як влаштований один запуск, константи, CI.
- [docs/AUDIT.md](docs/AUDIT.md) — знахідки аудиту й пріоритети.

Зміни — лише через гілку й Pull Request; мерж у `main` після погодження власника.
