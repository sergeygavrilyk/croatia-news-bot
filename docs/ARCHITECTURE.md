# Архітектура Croatia News Bot

Стан на 30.10.2026, коміт `50d42ab` (код востаннє змінено 21.10.2026, `0556d7f`). Номери рядків — у `news_bot.py`; після змін коду їх треба оновити.

## 1. Компоненти

| Компонент | Роль |
|---|---|
| cron-job.org | За розкладом `0 9-18,21 * * *` (місцевий час, 11 запусків/добу) робить HTTP POST `workflow_dispatch` у GitHub API. Обрано замість вбудованого розкладу GitHub, бо той запускав із затримками (R-02) |
| GitHub Actions `.github/workflows/news_bot_run.yml` | Ставить Python 3.11, залежності, запускає `news_bot.py`, потім комітить файли стану в ту саму гілку |
| `news_bot.py` (1543 рядки) | Увесь бот: збір RSS, фільтр Gemini, переклад, публікація, звіт |
| `feeds.json` | Список RSS-джерел (`name`, `url`, `category`) |
| Файли стану (у git) | `posted_news.txt` (URL опублікованого, ≤1000), `posted_titles.txt` (оригінальні заголовки, ≤1000), `news_backlog.json` (черга), `analytics.json` (100 останніх запусків) |
| Google Gemini (AI Studio, free tier) | Редакторський фільтр і переклад; структуровані JSON-відповіді (pydantic-схеми) |
| Telegram Bot API | Публікація в канал (`sendMessage` / `sendPhoto` / `sendMediaGroup`), звіт і алерти в адмін-чат |
| `check_models.py`, `ping_gemini.py`, `test_rss.py` | Ручні локальні утиліти (список моделей, пінг моделі, доступність RSS) |

## 2. Один запуск: покроково

### 2.0 `main()` (р. 1500)
Перевіряє `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `GEMINI_API_KEY` → створює клієнт Gemini → `scrape_feeds()` → `refresh_backlog_stats()` → `process_backlog()`. У `finally`: обрізка історій до 1000 рядків, запис `analytics.json`, звіт адміну (лише якщо щось опубліковано або є помилки). **Код виходу 1, якщо `errors_count > 0` або критична помилка** → у workflow не виконується коміт стану (AUDIT A2).

### 2.1 Збір: `scrape_feeds()` (р. 1138)
- Для кожного джерела з `feeds.json`: завантаження через `cloudscraper` (обхід Cloudflare), `feedparser`, перші **15** записів.
- Відкидає: уже в історії URL/заголовків або в беклозі; старші за `MAX_AGE_SECONDS` (12 год); з тегами RSS `sport`, `lifestyle`, `magazin` тощо.
- Якщо опис коротший за 200 символів або обірваний — `fetch_full_article_text()` (р. 398) тягне абзаци зі сторінки (до 3500 символів).
- `is_quality_article()` (р. 472): URL-категорії, `SPORTS_PATTERN`, `AD_PATTERN`, `LEGACY_UNWANTED_KEYWORDS`, довжина заголовка, «!!!», КАПС > 40 %.
- Фото — `extract_image_urls()` (р. 377): `media_content` і `enclosures`, без webp/avif/svg, до 10.
- Беклог зберігається атомарно з резервною копією `.bak` (`save_backlog()`, р. 229).

### 2.2 Відбір і публікація: `process_backlog()` (р. 1296)
1. Прибирає з беклогу вже опубліковане; пропускає новини з `retry_after` у майбутньому.
2. Кандидати (до `MAX_CANDIDATES_PER_FILTER` = 25): спершу «український пріоритет» (`UKRAINE_KEYWORDS`, р. 1120), далі по колу між джерелами у випадковому порядку.
3. `deduplicate_news()` (р. 619) — **редакторський промпт** (корінна подія → кластер дублів → порівняння з 75 останніми заголовками історії → стоп-теми → тематика: Задар, Хорватія, Україна/біженці). Відповідь `{keep, rejected[{id, entities_found, reason}]}` перевіряється на повноту: кожен ID рівно в одному списку, інакше — алерт і зупинка без змін беклогу («fail closed»).
4. Перші `BATCH_SIZE` = 3 погоджені → пауза 15 с → `_build_translation_prompt()` (р. 1259, 12 правил перекладу) → `generate_news_texts_json()` (р. 603) → чистка гомогліфів/наголосів/трикрапок.
5. `validate_translation_strict()` (р. 812): заголовок без HTML; у тексті лише `<b><i><u><s>`; повторна перевірка на спорт/рекламу; 2–3 хештеги, перший — латиницею (географія); видимий текст ≤ 900 (з фото) / 2500 символів; без «...».
6. Невдалий переклад → `record_translation_failure()` (р. 798): `retry_after` = 1 год × N (макс. 6 год), після 5 спроб — алерт адміну (новина лишається в беклозі до 12 год віку).
7. Успіх → `send_to_telegram()` → запис URL і заголовка в історію → пауза 45 с.
8. З беклогу прибираються лише опубліковані й відхилені фільтром; решта лишається.

### 2.3 Telegram: `send_to_telegram()` (р. 999)
Пост = `<b>заголовок</b>` + текст + хештеги + `🔗 Джерело: <a>назва</a>`. Ліміт 1024 символи (з фото) / 4096 (без): якщо довше — HTML-безпечна обрізка тіла (`_truncate_html_visible()`, р. 950) з приміткою «Повні деталі… 👇», хештеги зберігаються. Фото: 1 → `sendPhoto`, 2–10 → `sendMediaGroup`. Якщо Telegram відхилив фото (4xx) — повтор текстом; при 429/5xx — без фолбеку (результат невідомий, щоб не задублювати). Помилка HTML-розмітки → повтор без HTML. Ретраї `tenacity`: 4 спроби, `retry_after` від Telegram (до 120 с).

### 2.4 Звіт адміну: `send_analytics_to_admin()` (р. 306)
Час виконання, помилки, повтори Gemini, воронка (переглянуто / уже відомі / відсіяно кодом / відхилено Gemini), причини відхилення з сутностями, беклог по джерелах, опубліковано (з фото / текстом / український пріоритет / джерела), використані моделі. Алерти `⚠️ ALARM` — `send_alert_to_admin()` (р. 290).

## 3. Моделі Gemini і повтори
`_generate_structured_response()` (р. 522): для кожної моделі з `GEMINI_MODELS` (р. 70: `gemini-3.5-flash-lite`, потім `gemini-3.6-flash`) — до 2 спроб; повтор на тій самій моделі лише при тимчасовій помилці (429, 5xx, таймаут), пауза 3 с; інакше — наступна модель. Safety-фільтри вимкнені. `temperature`: фільтр 0.0, переклад 0.1. Таймауту запиту немає. Відповідь валідується pydantic-схемою; для перекладу ще й збіг множини ID.

## 4. Дані (файли стану)
- Елемент беклогу: `title`, `description` (чистий текст), `url` (нормалізований, без `utm_*`, `fbclid`…), `image_urls[]`, `source`, `timestamp` (Unix, UTC), опційно `translation_failures`, `retry_after`, `last_translation_error`.
- `analytics.json` — масив із 100 останніх об'єктів статистики (`_empty_stats()`, р. 1464). `timestamp` — UTC раннера.
- Запис атомарний (`.tmp` + `os.replace`); беклог читається з `.bak`, якщо основний файл пошкоджений, інакше бот зупиняється (а не обнуляє чергу).

## 5. Ключові константи (р. 55–70)
| Константа | Значення | Сенс |
|---|---|---|
| `MAX_HISTORY_SIZE` | 1000 | рядків в історії URL і заголовків |
| `MAX_AGE_SECONDS` | 12 год | максимальний вік новини |
| `BATCH_SIZE` | 3 | постів за запуск |
| `MAX_CANDIDATES_PER_FILTER` | 25 | новин у запиті до фільтра |
| `PRE_TRANSLATION_DELAY_SECONDS` | 15 | пауза перед перекладом |
| `POST_PUBLICATION_DELAY_SECONDS` | 45 | пауза між постами |
| `MAX_TRANSLATION_FAILURES` | 5 | після скількох невдач — алерт |
| `RETRY_COOLDOWN_SECONDS` | 1 год | база для `retry_after` |

## 6. CI: `.github/workflows/news_bot_run.yml`
- Тригер: лише `workflow_dispatch` (його викликає cron-job.org).
- `environment`: `production` для `main`, `staging` для інших гілок (секрети можуть відрізнятися).
- `permissions: contents: write` — для коміту стану.
- Кроки: checkout@v4 → setup-python@v5 (3.11, кеш pip) → `pip install -r requirements.txt` → `python news_bot.py` → `git add` 4 файли стану → коміт `Update news history and backlog state [skip ci]` → `git pull --rebase` → `git push`.
- Немає `concurrency`, `timeout-minutes`; крок коміту не виконується після падіння бота (AUDIT A2, A3).
