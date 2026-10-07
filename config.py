# -*- coding: utf-8 -*-
"""
Конфигурация. Ничего не хардкодим — все секреты через переменные окружения
(файл .env рядом с проектом, см. .env.example).
"""
import os

MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "")

# API "Автохаб для партнёров" (maxposter.ru) — для автоподтяжки данных по ссылке на оценку.
MAXPOSTER_API_KEY = os.environ.get("MAXPOSTER_API_KEY", "")
MAXPOSTER_API_BASE = os.environ.get("MAXPOSTER_API_BASE", "https://api.maxposter.ru")

# Google Sheets — для истории/отчётности
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_TAB = os.environ.get("GOOGLE_SHEET_TAB", "История оценок")
# История оценок по метрике v2.0 — отдельная вкладка (старая остаётся как архив v1)
GOOGLE_SHEET_TAB_V2 = os.environ.get("GOOGLE_SHEET_TAB_V2", "История оценок v2")

# Владелец бота — единственный, кто может полностью включать/выключать бота
# для всех остальных (например, если Ринат уйдёт из компании). Не путать
# с ADMIN_USER_IDS — те могут одобрять заявки, но не могут отключить бота.
OWNER_USER_ID = int(os.environ.get("OWNER_USER_ID", "4852272"))

# Кто может пользоваться ботом без ограничений (администраторы).
# Указывайте MAX user_id через запятую, например "123456,789012"
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip().isdigit()
}

# Устаревший способ ограничения доступа (список ID в .env, требует
# перезапуска при изменении). Теперь основной способ — access_store.py
# с заявками на доступ через /pending и /approve, но эта переменная
# всё ещё поддерживается для совместимости.
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
}

# Напоминания согласующим: если сделка ждёт решения дольше REMINDER_AFTER_MINUTES,
# бот повторно присылает карточку с кнопками. Только в рабочие часы (время сервера),
# REMINDER_HOURS="9-21" — с 9:00 до 21:00. REMINDER_AFTER_MINUTES=0 — выключить.
REMINDER_AFTER_MINUTES = int(os.environ.get("REMINDER_AFTER_MINUTES", "120") or 0)
_hours = os.environ.get("REMINDER_HOURS", "9-21").split("-")
REMINDER_HOURS = (int(_hours[0]), int(_hours[1])) if len(_hours) == 2 else (9, 21)
