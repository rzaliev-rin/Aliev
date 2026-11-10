# -*- coding: utf-8 -*-
"""
Вход в бота по корпоративной почте: сотрудник пишет адрес @expocar.ru,
бот отправляет на него 6-значный код, сотрудник вводит код — доступ открыт
без ручного одобрения администратором.

Почта отправляется через SMTP-ящик, который заводит IT (настройки в .env:
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM). Если SMTP не задан,
бот работает по-старому — заявка администратору с кнопками «Одобрить/Отклонить».
Коды хранятся в email_codes.json только в виде хэша.
"""
import datetime
import hashlib
import json
import os
import re
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional, Tuple

from config import (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_SECURITY,
                    ACCESS_EMAIL_DOMAINS)

STORE_PATH = os.path.join(os.path.dirname(__file__), "email_codes.json")
CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5
RESEND_SECONDS = 60
MAX_SENDS_PER_DAY = 10

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9\-]+\.)+[A-Za-z]{2,}$")
CODE_RE = re.compile(r"^\d{6}$")


def enabled() -> bool:
    return bool(SMTP_HOST and ACCESS_EMAIL_DOMAINS)


def domains_text() -> str:
    return ", ".join("@" + d for d in ACCESS_EMAIL_DOMAINS)


def looks_like_email(text: str) -> bool:
    return bool(EMAIL_RE.match((text or "").strip()))


def looks_like_code(text: str) -> bool:
    return bool(CODE_RE.match(re.sub(r"\s+", "", text or "")))


def corporate(email: str) -> bool:
    return email.strip().lower().rsplit("@", 1)[-1] in ACCESS_EMAIL_DOMAINS


def _load() -> dict:
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — нет файла или он битый: начинаем с пустого
        return {}


def _save(data: dict) -> None:
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def _hash(user_id: int, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}".encode()).hexdigest()


def _now() -> datetime.datetime:
    return datetime.datetime.now()


def pending_email(user_id: int) -> Optional[str]:
    """Адрес, на который отправлен ещё действующий код (или None)."""
    entry = _load().get(str(user_id))
    if not entry or datetime.datetime.fromisoformat(entry["expires"]) < _now():
        return None
    return entry["email"]


def _send_mail(to: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"Код входа в бот «Метрика_оценка АСП»: {code}"
    msg["From"] = formataddr(("Бот Метрика_оценка АСП", SMTP_FROM or SMTP_USER))
    msg["To"] = to
    msg.set_content(
        f"Ваш код для входа в бот «Метрика_оценка АСП» в MAX: {code}\n\n"
        f"Введите его в чате с ботом в течение {CODE_TTL_MINUTES} минут.\n"
        "Если вы не запрашивали код — просто удалите это письмо.")
    context = ssl.create_default_context()
    if SMTP_SECURITY == "ssl":
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=30) as smtp:
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            if SMTP_SECURITY == "starttls":
                smtp.starttls(context=context)
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)


def send_code(user_id: int, email: str) -> Tuple[bool, str]:
    """Отправляет код. Возвращает (успех, текст для пользователя)."""
    email = email.strip().lower()
    if not corporate(email):
        return False, f"Нужна корпоративная почта ({domains_text()}). Введите рабочий адрес."
    data = _load()
    key = str(user_id)
    entry = data.get(key) or {}
    now = _now()
    if entry.get("sent_at"):
        wait = RESEND_SECONDS - (now - datetime.datetime.fromisoformat(entry["sent_at"])).total_seconds()
        if wait > 0:
            return False, f"Код уже отправлен. Повторно можно запросить через {int(wait) + 1} сек."
    day = now.date().isoformat()
    sends = entry.get("sends", 0) if entry.get("day") == day else 0
    if sends >= MAX_SENDS_PER_DAY:
        return False, "Слишком много запросов кода за сегодня. Попробуйте завтра или обратитесь к администратору."
    code = f"{secrets.randbelow(10 ** 6):06d}"
    try:
        _send_mail(email, code)
    except Exception as e:  # noqa: BLE001
        return False, f"⚠️ Не получилось отправить письмо ({type(e).__name__}). Попробуйте позже или обратитесь к администратору."
    data[key] = {"email": email, "hash": _hash(user_id, code), "attempts": 0,
                 "sent_at": now.isoformat(timespec="seconds"),
                 "expires": (now + datetime.timedelta(minutes=CODE_TTL_MINUTES)).isoformat(timespec="seconds"),
                 "day": day, "sends": sends + 1}
    _save(data)
    return True, (f"📧 На {email} отправлен код из 6 цифр. Введите его сюда в течение {CODE_TTL_MINUTES} минут.\n"
                  "Письма нет — проверьте «Спам» или пришлите адрес ещё раз.")


def check_code(user_id: int, code: str) -> Tuple[Optional[str], str]:
    """(email, текст): email — если код верный (доступ можно открывать)."""
    code = re.sub(r"\s+", "", code or "")
    data = _load()
    key = str(user_id)
    entry = data.get(key)
    if not entry:
        return None, f"Сначала введите корпоративную почту ({domains_text()}) — на неё придёт код."
    if datetime.datetime.fromisoformat(entry["expires"]) < _now():
        data.pop(key, None)
        _save(data)
        return None, "Код истёк. Пришлите адрес почты ещё раз — придёт новый код."
    if entry["attempts"] >= MAX_ATTEMPTS:
        return None, "Слишком много неверных попыток. Пришлите адрес почты ещё раз — придёт новый код."
    if not secrets.compare_digest(entry["hash"], _hash(user_id, code)):
        entry["attempts"] += 1
        _save(data)
        left = MAX_ATTEMPTS - entry["attempts"]
        return None, f"Неверный код. Осталось попыток: {left}." if left else \
            "Неверный код. Пришлите адрес почты ещё раз — придёт новый код."
    data.pop(key, None)
    _save(data)
    return entry["email"], "ok"
