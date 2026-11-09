# -*- coding: utf-8 -*-
"""
Профили поиска — по одному (или несколько) на салон.

Профиль = что ищем (фильтры), где ищем (площадки и ссылки поиска), с какого
аккаунта пишем, от чьего имени говорит агент и в какой рабочий чат MAX уходят
готовые к выкупу клиенты.

Настраивается командами в боте (см. buyout/bot_commands.py):
  /buyout                        — список профилей с кнопками
  /buyout_new <id> <Салон>       — создать профиль
  /buyout_set <id> ключ=значение — изменить параметр (можно несколько через ;)
  /buyout_chat <id>              — выполнить в рабочем чате: лиды пойдут сюда
"""
import copy
import os
import re
from typing import Optional

from buyout import jsonstore

STORE_PATH = os.environ.get(
    "BUYOUT_PROFILES_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "buyout_profiles.json"))

PLATFORMS = ("avito", "autoru", "drom")
PLATFORM_TITLES = {"avito": "Авито", "autoru": "Авто.ру", "drom": "Дром"}

DEFAULT_PROFILE = {
    "id": "",
    "salon": "",
    "enabled": False,          # включается вручную, когда всё настроено
    "chat_id": None,           # рабочий чат MAX для лидов
    # Площадки: ссылка поиска (фильтры, настроенные прямо на сайте) и аккаунт,
    # с которого пишем. Аккаунт — имя сохранённой сессии браузера (см. README).
    "platforms": {p: {"enabled": False, "search_url": "", "account": ""} for p in PLATFORMS},
    # Дополнительные фильтры бота — применяются к каждому найденному объявлению
    "filters": {
        "brands": [],            # пусто — любые
        "models": [],
        "year_from": None, "year_to": None,
        "price_from": None, "price_to": None,
        "mileage_max": None,
        "owners_max": None,
        "transmission": [],      # "автомат", "механика", "робот", "вариатор"
        "only_private": True,    # только частные продавцы (не салоны/перекупы)
        "stop_words": ["битый", "не на ходу", "на запчасти", "после дтп", "залог", "обмен"],
    },
    # От чьего имени и с чем пишет агент
    "pitch": {
        "manager_name": "",      # имя, которым представляется агент (например «Алексей»)
        "salon_title": "",       # как называть салон клиенту
        "address": "",
        "advantages": ["выкуп за один день", "деньги сразу на карту или наличными",
                       "все документы оформляем сами", "можем выкупить авто в кредите"],
        "extra": "",             # любые доп. инструкции агенту
    },
    "limits": {
        "first_messages_per_day": 25,   # новых продавцов в сутки на аккаунт
        "hours": "9-21",                # когда писать (время сервера)
        "reply_delay_sec": [60, 240],   # пауза перед ответом — как у живого человека
        "first_delay_sec": [120, 600],  # пауза между первыми сообщениями разным продавцам
    },
}

# Короткие имена для /buyout_set → путь в профиле
_KEYS = {
    "salon": ("salon",),
    "brands": ("filters", "brands"), "models": ("filters", "models"),
    "year_from": ("filters", "year_from"), "year_to": ("filters", "year_to"),
    "price_from": ("filters", "price_from"), "price_to": ("filters", "price_to"),
    "mileage_max": ("filters", "mileage_max"), "owners_max": ("filters", "owners_max"),
    "transmission": ("filters", "transmission"), "only_private": ("filters", "only_private"),
    "stop_words": ("filters", "stop_words"),
    "manager_name": ("pitch", "manager_name"), "salon_title": ("pitch", "salon_title"),
    "address": ("pitch", "address"), "advantages": ("pitch", "advantages"), "extra": ("pitch", "extra"),
    "per_day": ("limits", "first_messages_per_day"), "hours": ("limits", "hours"),
}
for _p in PLATFORMS:
    _KEYS[f"{_p}_url"] = ("platforms", _p, "search_url")
    _KEYS[f"{_p}_account"] = ("platforms", _p, "account")
    _KEYS[f"{_p}_on"] = ("platforms", _p, "enabled")

SETTABLE_KEYS = tuple(_KEYS)
_LIST_KEYS = {"brands", "models", "transmission", "stop_words", "advantages"}
_INT_KEYS = {"year_from", "year_to", "price_from", "price_to", "mileage_max", "owners_max", "per_day"}
_BOOL_KEYS = {"only_private"} | {f"{p}_on" for p in PLATFORMS}


def _merge(default, data):
    """Дополняет сохранённый профиль новыми полями по умолчанию (после обновлений бота)."""
    if not isinstance(default, dict) or not isinstance(data, dict):
        return copy.deepcopy(data)
    out = copy.deepcopy(default)
    for k, v in data.items():
        out[k] = _merge(default[k], v) if k in default else copy.deepcopy(v)
    return out


def _load() -> dict:
    data = jsonstore.load(STORE_PATH, {"profiles": {}})
    data.setdefault("profiles", {})
    return data


def all_profiles() -> list:
    return [_merge(DEFAULT_PROFILE, p) for p in _load()["profiles"].values()]


def get(profile_id: str) -> Optional[dict]:
    p = _load()["profiles"].get(profile_id)
    return _merge(DEFAULT_PROFILE, p) if p else None


def save(profile: dict) -> None:
    data = _load()
    data["profiles"][profile["id"]] = profile
    jsonstore.save(STORE_PATH, data)


def create(profile_id: str, salon: str) -> dict:
    if not re.fullmatch(r"[a-z0-9_\-]{2,32}", profile_id):
        raise ValueError("ID профиля — латиница, цифры, «-» или «_», 2–32 символа, например tmn-toyota")
    if get(profile_id):
        raise ValueError(f"Профиль {profile_id} уже есть")
    profile = copy.deepcopy(DEFAULT_PROFILE)
    profile.update(id=profile_id, salon=salon)
    profile["pitch"]["salon_title"] = salon
    save(profile)
    return profile


def delete(profile_id: str) -> bool:
    data = _load()
    if data["profiles"].pop(profile_id, None) is None:
        return False
    jsonstore.save(STORE_PATH, data)
    return True


def parse_value(key: str, raw: str):
    raw = raw.strip()
    if key in _LIST_KEYS:
        return [x.strip() for x in raw.split(",") if x.strip()]
    if key in _INT_KEYS:
        if raw.lower() in ("", "-", "нет", "любой", "любая"):
            return None
        digits = re.sub(r"[\s_]", "", raw).lower().replace("тыс", "000").replace("млн", "000000")
        if not re.fullmatch(r"\d+", digits):
            raise ValueError(f"{key}: ожидается число, получено «{raw}»")
        return int(digits)
    if key in _BOOL_KEYS:
        if raw.lower() in ("1", "да", "yes", "on", "true", "вкл"):
            return True
        if raw.lower() in ("0", "нет", "no", "off", "false", "выкл"):
            return False
        raise ValueError(f"{key}: ожидается да/нет")
    return raw


def set_values(profile_id: str, assignments: str) -> dict:
    """assignments: «year_from=2016; price_to=2 500 000; brands=Toyota,Lexus»."""
    profile = get(profile_id)
    if not profile:
        raise ValueError(f"Профиль {profile_id} не найден")
    changed = []
    for part in re.split(r"[;\n]", assignments):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"Не понял «{part.strip()}» — нужно ключ=значение")
        key, raw = part.split("=", 1)
        key = key.strip().lower()
        if key not in _KEYS:
            raise ValueError(f"Неизвестный параметр «{key}». Доступны: {', '.join(SETTABLE_KEYS)}")
        node = profile
        path = _KEYS[key]
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = parse_value(key, raw)
        changed.append(key)
    if not changed:
        raise ValueError("Нечего менять")
    save(profile)
    return profile


def set_enabled(profile_id: str, enabled: bool) -> dict:
    profile = get(profile_id)
    if not profile:
        raise ValueError(f"Профиль {profile_id} не найден")
    profile["enabled"] = enabled
    save(profile)
    return profile


def set_chat(profile_id: str, chat_id: int) -> dict:
    profile = get(profile_id)
    if not profile:
        raise ValueError(f"Профиль {profile_id} не найден")
    profile["chat_id"] = chat_id
    save(profile)
    return profile


def problems(profile: dict) -> list:
    """Чего не хватает, чтобы профиль мог работать."""
    out = []
    if not profile.get("chat_id"):
        out.append("не привязан рабочий чат (/buyout_chat в нужном чате)")
    active = [p for p, cfg in profile["platforms"].items() if cfg.get("enabled")]
    if not active:
        out.append("не включена ни одна площадка (avito_on=да и т.п.)")
    for p in active:
        cfg = profile["platforms"][p]
        if not cfg.get("search_url"):
            out.append(f"{PLATFORM_TITLES[p]}: нет ссылки поиска ({p}_url=...)")
        if not cfg.get("account"):
            out.append(f"{PLATFORM_TITLES[p]}: не указан аккаунт ({p}_account=...)")
    if not profile["pitch"].get("manager_name"):
        out.append("не задано имя, от которого пишет агент (manager_name=...)")
    return out


def describe(profile: dict) -> str:
    f, pitch, lim = profile["filters"], profile["pitch"], profile["limits"]

    def rng(a, b, unit=""):
        if a is None and b is None:
            return "любой"
        return f"{a if a is not None else '…'}–{b if b is not None else '…'}{unit}"

    lines = [
        f"🚗 Профиль {profile['id']} — {profile['salon']}",
        f"Статус: {'🟢 работает' if profile['enabled'] else '⏸ выключен'}",
        f"Рабочий чат: {profile['chat_id'] or 'не привязан'}",
        "",
        "Площадки:",
    ]
    for p in PLATFORMS:
        cfg = profile["platforms"][p]
        mark = "✅" if cfg.get("enabled") else "▫️"
        lines.append(f"{mark} {PLATFORM_TITLES[p]}: аккаунт «{cfg.get('account') or '—'}», "
                     f"поиск: {'задан' if cfg.get('search_url') else 'нет'}")
    lines += [
        "",
        "Фильтры:",
        f"Марки: {', '.join(f['brands']) or 'любые'}; модели: {', '.join(f['models']) or 'любые'}",
        f"Год: {rng(f['year_from'], f['year_to'])}; цена: {rng(f['price_from'], f['price_to'], ' ₽')}",
        f"Пробег до: {f['mileage_max'] or 'любой'}; владельцев до: {f['owners_max'] or 'любое число'}",
        f"КПП: {', '.join(f['transmission']) or 'любая'}; только частники: {'да' if f['only_private'] else 'нет'}",
        f"Стоп-слова: {', '.join(f['stop_words']) or '—'}",
        "",
        f"Агент представляется: {pitch['manager_name'] or '—'}, {pitch['salon_title'] or profile['salon']}",
        f"Лимит: {lim['first_messages_per_day']} новых продавцов в день на аккаунт, часы {lim['hours']}",
    ]
    issues = problems(profile)
    if issues:
        lines += ["", "⚠️ Не хватает:"] + [f"• {x}" for x in issues]
    return "\n".join(lines)
