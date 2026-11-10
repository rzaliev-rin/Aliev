# -*- coding: utf-8 -*-
"""
Отдельный бот MAX «Выкуп и комиссия» — ИИ-агент пишет продавцам на Авито /
Авто.ру / Дром и передаёт готовых клиентов в рабочие чаты салонов.

С ботом «Метрика_оценка АСП» не связан: свой токен, своя папка, своя служба.
Запуск: python -m buyout.main  (настройки — файл .env рядом с папкой buyout/)
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
if os.environ.get("TZ") and hasattr(time, "tzset"):
    time.tzset()  # рабочие часы агента — по времени региона, а не сервера

from maxgram import Bot, Dispatcher, Router  # noqa: E402
from maxgram.filters import Command, CommandObject  # noqa: E402
from maxgram.utils.keyboard import InlineKeyboardBuilder  # noqa: E402

from buyout import bot_commands, jsonstore  # noqa: E402
from buyout.platforms.browser import SELECTORS, session_path  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("buyout")

TOKEN = os.environ.get("BUYOUT_BOT_TOKEN") or os.environ.get("MAX_BOT_TOKEN", "")
if not TOKEN:
    raise SystemExit("Не задан BUYOUT_BOT_TOKEN в .env — токен бота от @MasterBot в MAX")
OWNER_USER_ID = int(os.environ.get("OWNER_USER_ID", "0") or 0)
ADMINS_PATH = os.path.join(ROOT, "buyout_admins.json")

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()


# --- доступ ---------------------------------------------------------------

def _admins() -> set:
    env = {int(x) for x in os.environ.get("BUYOUT_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
    return env | set(jsonstore.load(ADMINS_PATH, {"admins": []})["admins"])


def is_owner(uid: Optional[int]) -> bool:
    return uid is not None and uid == OWNER_USER_ID


def is_admin(uid: Optional[int]) -> bool:
    return uid is not None and (is_owner(uid) or uid in _admins())


def _uid(obj) -> Optional[int]:
    sender = getattr(obj, "sender", None) or getattr(obj, "user", None)
    return getattr(sender, "user_id", None)


def _is_private(message) -> bool:
    recipient = getattr(message, "recipient", None)
    chat_type = getattr(recipient, "chat_type", None)
    if chat_type is not None:
        return str(chat_type).lower() in ("dialog", "private", "user")
    return getattr(recipient, "chat_id", None) == _uid(message)


# --- связь с движком --------------------------------------------------------

async def send_to_owner(text: str) -> None:
    if not OWNER_USER_ID:
        logger.warning("OWNER_USER_ID не задан, сообщение владельцу не отправлено: %s", text)
        return
    try:
        await bot.send_message(user_id=OWNER_USER_ID, text=text[:3900])
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось написать владельцу: %s", e)


async def notify(chat_id: int, text: str) -> None:
    """Карточка клиента в рабочий чат; не получилось — владельцу, чтобы клиент не потерялся."""
    try:
        await bot.send_message(chat_id=chat_id, text=text[:3900])
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось отправить в чат %s: %s", chat_id, e)
        await send_to_owner(f"⚠️ Не удалось отправить в рабочий чат {chat_id} (бот добавлен в чат?):\n\n{text}")


async def alert(text: str) -> None:
    await send_to_owner(f"🛠 Бот выкупа: {text}")


# --- меню -------------------------------------------------------------------

MENU_TEXT = (
    "🚗 Бот «Выкуп и комиссия»\n\n"
    "ИИ-агент находит машины частников на Авито, Авто.ру и Дроме, пишет продавцам от имени салона, "
    "предлагает выкуп или комиссию и отправляет готовых клиентов в рабочий чат салона.\n\n"
    "Основное:\n"
    "/buyout — профили салонов, статистика, включить/выключить\n"
    "/buyout_new <id> <Салон> — новый профиль\n"
    "/buyout_set <id> ключ=значение; … — настройки (/buyout_help — все параметры)\n"
    "/buyout_chat <id> — выполнить в рабочем чате салона (бот должен быть добавлен в чат)\n"
    "/buyout_try <id> [выкуп|комиссия] — песочница: агент пишет вам как продавцу, ответ — /t текст\n"
    "/buyout_mute <ID диалога> — забрать диалог у агента\n"
    "/bitrix_check — проверить Битрикс24 и узнать ID сотрудников\n\n"
    "Вход в аккаунт площадки: на компьютере выполните python -m buyout.login drom <аккаунт> "
    "и пришлите сюда получившийся файл buyout_session_….json.\n\n"
    "Владелец: /admins, /admin_add <ID>, /admin_del <ID>. Ваш ID: /myid"
)


def menu_kb():
    kb = InlineKeyboardBuilder()
    kb.callback(text="🚗 Профили и статистика", payload="bo_list")
    kb.adjust(1)
    return kb


@router.message(Command("start"))
@router.message(Command("menu"))
@router.message(Command("help"))
async def cmd_start(message, bot):
    uid = _uid(message)
    if not is_admin(uid):
        await message.answer(text=f"Это служебный бот отдела выкупа. Ваш ID: {uid} — "
                                  "передайте его владельцу бота, чтобы получить доступ.")
        return
    await message.answer(text=MENU_TEXT, keyboard=menu_kb())


@router.message(Command("myid"))
async def cmd_myid(message, bot):
    await message.answer(text=f"Ваш ID в MAX: {_uid(message)}\nID этого чата: {message.recipient.chat_id}")


@router.message(Command("bitrix_check"))
async def cmd_bitrix_check(message, bot):
    if not is_admin(_uid(message)):
        await message.answer(text="Только для администраторов.")
        return
    from buyout import bitrix
    await message.answer(text=await asyncio.to_thread(bitrix.check))


@router.message(Command("admins"))
async def cmd_admins(message, bot):
    if not is_owner(_uid(message)):
        await message.answer(text="Только для владельца бота.")
        return
    ids = sorted(_admins())
    await message.answer(text="Администраторы: " + (", ".join(map(str, ids)) if ids else "пока только владелец"))


async def _change_admin(message, command: CommandObject, add: bool):
    if not is_owner(_uid(message)):
        await message.answer(text="Только для владельца бота.")
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer(text=f"Использование: /admin_{'add' if add else 'del'} <ID в MAX> (ID — команда /myid)")
        return
    data = jsonstore.load(ADMINS_PATH, {"admins": []})
    ids = set(data["admins"])
    (ids.add if add else ids.discard)(int(arg))
    data["admins"] = sorted(ids)
    jsonstore.save(ADMINS_PATH, data)
    await message.answer(text=f"Готово. Администраторы: {', '.join(map(str, data['admins'])) or 'только владелец'}")


@router.message(Command("admin_add"))
async def cmd_admin_add(message, command: CommandObject, bot):
    await _change_admin(message, command, True)


@router.message(Command("admin_del"))
async def cmd_admin_del(message, command: CommandObject, bot):
    await _change_admin(message, command, False)


# --- файл входа в аккаунт площадки -----------------------------------------

SESSION_NAME = re.compile(r"^buyout_session_(avito|autoru|drom)_([a-z0-9_\-]{1,40})\.json$", re.I)


def _session_attachment(message):
    forwarded = getattr(getattr(message, "link", None), "message", None)  # файл можно и переслать
    attachments = list(getattr(getattr(message, "body", None), "attachments", None) or []) \
        + list(getattr(forwarded, "attachments", None) or [])
    for att in attachments:
        name = getattr(att, "filename", None) or ""
        url = getattr(getattr(att, "payload", None), "url", None)
        m = SESSION_NAME.match(name)
        if getattr(att, "type", None) == "file" and url and m:
            return m.group(1).lower(), m.group(2), url
    return None


def save_session(platform: str, account: str, url: str) -> str:
    import requests
    try:
        resp = requests.get(url, timeout=(15, 60))
        resp.raise_for_status()
        state = json.loads(resp.content.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"⚠️ Не получилось прочитать файл: {e}"
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not state["cookies"]:
        return "⚠️ В файле нет входа в аккаунт. Повторите python -m buyout.login и войдите до конца."
    path = session_path(platform, account)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.chmod(path, 0o600)
    return (f"✅ Вход сохранён: {platform} / {account}. Укажите его в профиле: "
            f"/buyout_set <id> {platform}_account={account}\nСообщение с файлом лучше удалить из чата.")


@router.message()
async def on_message(message, bot):
    if not _is_private(message):
        return
    found = _session_attachment(message)
    if found:
        if not is_owner(_uid(message)) and not is_admin(_uid(message)):
            return
        platform, account, url = found
        text = await asyncio.to_thread(save_session, platform, account, url)
        if text.startswith("✅"):
            from buyout.platforms import get_platform
            await get_platform(platform).reset_account(account)
        await message.answer(text=text)
        return
    if is_admin(_uid(message)):
        await message.answer(text="Команды — /menu", keyboard=menu_kb())


@router.errors()
async def on_error(event, bot=None):
    exc = getattr(event, "exception", event)
    logger.exception("Необработанная ошибка: %r", exc, exc_info=exc)
    await alert(f"ошибка при обработке сообщения: {type(exc).__name__}: {exc}")
    return True


dp.include_router(bot_commands.setup(is_admin))  # команды /buyout… — раньше «поймать всё»
dp.include_router(router)


async def _set_commands():
    from maxgram.types import BotCommand
    try:
        await bot.set_commands([BotCommand(name=n, description=d) for n, d in [
            ("menu", "Меню и справка"),
            ("buyout", "Профили салонов и статистика"),
            ("buyout_help", "Все параметры настройки"),
            ("bitrix_check", "Проверить Битрикс24"),
            ("myid", "Мой ID в MAX"),
        ]])
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось задать меню команд: %s", e)


async def _main():
    await _set_commands()
    tasks = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        from buyout.engine import BuyoutEngine, run_forever
        tasks.append(asyncio.create_task(run_forever(BuyoutEngine(notify, alert))))
        await alert("✅ запущен. Агент работает по включённым профилям (/buyout).")
    else:
        await alert("запущен, но ANTHROPIC_API_KEY не задан в .env — агент не пишет продавцам. "
                    "Добавьте ключ и перезапустите: systemctl restart buyoutbot")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_main())
