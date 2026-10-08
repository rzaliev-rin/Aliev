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

# Напоминание согласующим: если сделка ждёт решения дольше REMINDER_AFTER_MINUTES,
# бот ОДИН раз повторно присылает карточку с кнопками (на каждом этапе: салон, УК, ПАЦ).
# Только в рабочие часы (время сервера), REMINDER_HOURS="9-21" — с 9:00 до 21:00.
# REMINDER_AFTER_MINUTES=0 — выключить напоминания полностью.
REMINDER_AFTER_MINUTES = int(os.environ.get("REMINDER_AFTER_MINUTES", "60") or 0)
_hours = os.environ.get("REMINDER_HOURS", "9-21").split("-")
REMINDER_HOURS = (int(_hours[0]), int(_hours[1])) if len(_hours) == 2 else (9, 21)

# --- Расписание (время сервера, ЧЧ:ММ; пусто — выключено) ---
# Утренняя сводка согласующим: что ждёт именно их решения
DIGEST_TIME = os.environ.get("DIGEST_TIME", "09:00").strip()
# Недельный отчёт: день недели (0 — понедельник) и время
REPORT_WEEKDAY = int(os.environ.get("REPORT_WEEKDAY", "0") or 0)
REPORT_TIME = os.environ.get("REPORT_TIME", "09:00").strip()
# Кому слать недельный отчёт (MAX ID через запятую); пусто — только владельцу
REPORT_USER_IDS = {
    int(x) for x in os.environ.get("REPORT_USER_IDS", "").split(",") if x.strip().isdigit()
} or {OWNER_USER_ID}
# Резервная копия данных бота (доступы, сделки, чаты, журнал) владельцу в MAX
BACKUP_TIME = os.environ.get("BACKUP_TIME", "21:30").strip()
BACKUP_KEEP_DAYS = int(os.environ.get("BACKUP_KEEP_DAYS", "14") or 14)

# Повторная оценка: за сколько дней искать оценки того же VIN
DUPLICATE_DAYS = int(os.environ.get("DUPLICATE_DAYS", "7") or 7)

# --- Поля MaxPoster, которых нет в стандартном наборе (уточняются по выводу /raw) ---
# Дата Авито-оценки/осмотра: путь к полю в ответе MaxPoster (например "createdAt"); пусто — автопоиск
MAXPOSTER_APPRAISAL_DATE_FIELD = os.environ.get("MAXPOSTER_APPRAISAL_DATE_FIELD", "").strip()
# Авито-оценка старше стольких дней — предупреждение (по метрике оценка нужна на день осмотра)
AVITO_MAX_AGE_DAYS = int(os.environ.get("AVITO_MAX_AGE_DAYS", "3") or 3)
# Тип приёма: путь к полю и соответствие значений, например
# MAXPOSTER_RECEPTION_FIELD=purchaseType
# MAXPOSTER_RECEPTION_MAP=trade_in:Trade-In,trade_up:Trade-Up,buyout:Выкуп с улицы,pi:Trade-In на ПИ
MAXPOSTER_RECEPTION_FIELD = os.environ.get("MAXPOSTER_RECEPTION_FIELD", "").strip()
MAXPOSTER_RECEPTION_MAP = {
    k.strip().lower(): v.strip()
    for k, v in (pair.split(":", 1) for pair in os.environ.get("MAXPOSTER_RECEPTION_MAP", "").split(",") if ":" in pair)
}
# ПЦП Автохаб (справочно, для «цена выкупа к ПЦП Автохаб»): путь к полю
MAXPOSTER_AUTOHUB_PTSP_FIELD = os.environ.get("MAXPOSTER_AUTOHUB_PTSP_FIELD", "").strip()
