# -*- coding: utf-8 -*-
"""
Простое файловое хранилище (JSON) для авторизации пользователей бота.
Не требует перезапуска сервиса при добавлении/удалении доступа —
изменения применяются сразу же при следующем сообщении.

"allowed" хранится как словарь {user_id: {"name": ..., "approved_at": ...}},
чтобы администратор видел не только голый ID, но и имя человека.
"""
import json
import os
import datetime
import secrets
from typing import Optional

STORE_PATH = os.path.join(os.path.dirname(__file__), "access_store.json")


def _load() -> dict:
    if not os.path.exists(STORE_PATH):
        return {"allowed": {}, "pending": {}, "bot_enabled": True}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("allowed", {})
        data.setdefault("pending", {})
        data.setdefault("bot_enabled", True)
        data.setdefault("invites", {})
        # миграция со старого формата (allowed как список ID без имён)
        if isinstance(data["allowed"], list):
            data["allowed"] = {str(uid): {"name": "", "approved_at": ""} for uid in data["allowed"]}
        return data
    except Exception:  # noqa: BLE001
        return {"allowed": {}, "pending": {}, "bot_enabled": True}


def _save(data: dict) -> None:
    # Пишем во временный файл и подменяем атомарно: если бот упадёт или
    # закончится место посреди записи, старый файл останется целым
    # (иначе _load прочитает битый JSON как пустой и следующая запись
    # сотрёт все данные).
    tmp_path = STORE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STORE_PATH)


def is_allowed(user_id: int) -> bool:
    data = _load()
    return str(user_id) in data["allowed"]


def request_access(user_id: int, name: Optional[str] = None) -> bool:
    """Регистрирует заявку на доступ. Возвращает True, если заявка новая."""
    data = _load()
    key = str(user_id)
    if key in data["pending"]:
        return False
    data["pending"][key] = {
        "name": name or "",
        "requested_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save(data)
    return True


def list_pending() -> dict:
    return _load()["pending"]


def approve(user_id: int, name: Optional[str] = None, email: Optional[str] = None) -> bool:
    """Одобряет заявку. Имя берётся из аргумента, либо из самой заявки (если была)."""
    data = _load()
    key = str(user_id)
    pending_entry = data["pending"].pop(key, None)
    resolved_name = name or (pending_entry or {}).get("name") or ""
    data["allowed"][key] = {
        "name": resolved_name,
        "approved_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if email:
        data["allowed"][key]["email"] = email
    _save(data)
    return True


def deny(user_id: int) -> bool:
    data = _load()
    key = str(user_id)
    existed = key in data["pending"]
    data["pending"].pop(key, None)
    _save(data)
    return existed


def list_allowed() -> dict:
    """Возвращает {user_id_str: {"name":..., "approved_at":...}}."""
    return _load()["allowed"]


def revoke(user_id: int) -> bool:
    """Убирает пользователя из списка разрешённых (например, при увольнении)."""
    data = _load()
    key = str(user_id)
    if key in data["allowed"]:
        data["allowed"].pop(key)
        _save(data)
        return True
    return False


def is_bot_enabled() -> bool:
    return _load().get("bot_enabled", True)


def set_bot_enabled(enabled: bool) -> None:
    data = _load()
    data["bot_enabled"] = enabled
    _save(data)


# ---------------------------------------------------------------- приглашения
INVITE_DAYS = 7


def create_invite(created_by: int, note: str = "") -> str:
    """Одноразовое приглашение: код вида inv_1a2b3c4d (действует INVITE_DAYS дней)."""
    data = _load()
    code = "inv_" + secrets.token_hex(4)
    data.setdefault("invites", {})[code] = {
        "created_by": created_by, "note": note,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "used_by": None,
    }
    _save(data)
    return code


def use_invite(code: str, user_id: int, name: Optional[str] = None) -> Optional[dict]:
    """Открывает доступ по приглашению. Возвращает приглашение или None (нет, использовано, истекло)."""
    data = _load()
    inv = data.get("invites", {}).get((code or "").strip().lower())
    if not inv or inv.get("used_by"):
        return None
    created = datetime.datetime.strptime(inv["created_at"], "%Y-%m-%d %H:%M:%S")
    if datetime.datetime.now() - created > datetime.timedelta(days=INVITE_DAYS):
        return None
    inv["used_by"] = user_id
    inv["used_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    key = str(user_id)
    data["pending"].pop(key, None)
    data["allowed"][key] = {"name": name or inv.get("note") or "",
                            "approved_at": inv["used_at"], "via": "приглашение"}
    _save(data)
    return inv

