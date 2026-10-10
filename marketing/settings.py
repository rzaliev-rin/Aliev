# -*- coding: utf-8 -*-
"""
Настройки маркетингового модуля. Секреты — только через .env (см. .env.example).
"""
import os

try:  # .env подхватываем так же, как бот
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# MaxPoster: тот же служебный аккаунт, что и у бота (base64("email:пароль"))
MAXPOSTER_API_KEY = os.environ.get("MAXPOSTER_API_KEY", "")
MAXPOSTER_API_BASE = os.environ.get("MAXPOSTER_API_BASE", "https://api.maxposter.ru")
# Путь метода склада. Не подтверждён вживую — сверить с partners-api-docs и
# при необходимости поправить в .env (формат тела запроса как у /appraisals).
MAXPOSTER_VEHICLES_PATH = os.environ.get("MAXPOSTER_VEHICLES_PATH", "/partners-api/vehicles")
MAXPOSTER_DEALER_FIELD = os.environ.get("MAXPOSTER_DEALER_FIELD", "dealer.id")
MARKETING_DEALER_ID = os.environ.get("MARKETING_DEALER_ID", "13463")  # ПИ Toyota Екатеринбург
MARKETING_DEALER_NAME = os.environ.get("MARKETING_DEALER_NAME", "ПИ Toyota Екатеринбург")

# Яндекс Вордстат API (api.wordstat.yandex.net). OAuth-токен приложения
# с доступом к Wordstat API — выдаётся по заявке в Яндексе.
WORDSTAT_TOKEN = os.environ.get("WORDSTAT_TOKEN", "")
WORDSTAT_API_BASE = os.environ.get("WORDSTAT_API_BASE", "https://api.wordstat.yandex.net")
# 54 — Екатеринбург, 11162 — Свердловская область
WORDSTAT_REGIONS = [
    int(x) for x in os.environ.get("WORDSTAT_REGIONS", "54").split(",") if x.strip().isdigit()
]
WORDSTAT_NUM_PHRASES = int(os.environ.get("WORDSTAT_NUM_PHRASES", "100") or 100)
# пауза между запросами, сек (у API есть квота запросов в секунду/сутки)
WORDSTAT_PAUSE = float(os.environ.get("WORDSTAT_PAUSE", "0.6") or 0.6)
MARKETING_CACHE_DIR = os.environ.get("MARKETING_CACHE_DIR", "marketing_cache")

CITY = os.environ.get("MARKETING_CITY", "Екатеринбург")
CITY_FORMS = ["екатеринбург", "екб", "екатеринбурге", "свердловская"]

# Google: папка «Маркетинг ПИ» и таблица отчёта (доступ на редактирование —
# email сервисного аккаунта из service_account.json)
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
MARKETING_SHEET_ID = os.environ.get("MARKETING_SHEET_ID", "")
