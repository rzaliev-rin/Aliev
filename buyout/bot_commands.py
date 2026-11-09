# -*- coding: utf-8 -*-
"""
Команды выкупа и комиссии в боте MAX (только для администраторов).

  /buyout                         — профили, статистика, справка
  /buyout_new <id> <Салон>        — создать профиль
  /buyout_set <id> ключ=значение; ключ=значение
  /buyout_show <id>               — все настройки профиля
  /buyout_chat <id>               — выполнить в рабочем чате: лиды пойдут сюда
  /buyout_on <id> | /buyout_off <id>
  /buyout_del <id>
  /buyout_try <id> [выкуп|комиссия] — песочница: агент пишет вам как продавцу
  /t <текст>                      — ответ агенту в песочнице от имени продавца
  /buyout_mute <ID диалога>       — агент больше не отвечает в этом диалоге (ведёт менеджер)
"""
import asyncio
import logging
from typing import Callable, Optional

from maxgram import F, Router
from maxgram.filters import Command, CommandObject
from maxgram.utils.keyboard import InlineKeyboardBuilder

from buyout import agent, dialogs, profiles
from buyout.listings import Listing

logger = logging.getLogger(__name__)

router = Router()
_is_admin: Callable[[Optional[int]], bool] = lambda uid: False  # noqa: E731
_sandboxes = {}  # user_id → {"profile_id", "dialog"}

HELP = (
    "Параметры для /buyout_set (через «;»):\n"
    "offer — что предлагаем: выкуп, комиссия или оба\n"
    "commission_terms — условия комиссии (размер, срок договора), commission_advantages — плюсы комиссии\n"
    "commission_price_to — машины дороже price_to, но до этой суммы, — только на комиссию\n"
    "salon, manager_name, salon_title, address, advantages (через запятую), extra\n"
    "brands, models (через запятую), year_from, year_to, price_from, price_to,\n"
    "mileage_max, owners_max, transmission, only_private (да/нет), stop_words\n"
    "avito_on / autoru_on / drom_on (да/нет)\n"
    "avito_url / autoru_url / drom_url — ссылка поиска с фильтрами прямо с сайта\n"
    "avito_account / autoru_account / drom_account — имя сохранённого входа\n"
    "per_day — новых продавцов в день, hours — часы работы, например 9-21\n\n"
    "Пример:\n/buyout_set tmn-toyota brands=Toyota,Lexus; year_from=2016; price_to=3 500 000; "
    "mileage_max=150000; manager_name=Алексей; offer=оба; commission_price_to=8 млн; "
    "commission_terms=комиссия 5% от цены продажи, договор на 45 дней"
)


def setup(is_admin: Callable[[Optional[int]], bool]) -> Router:
    global _is_admin
    _is_admin = is_admin
    return router


def _uid(obj) -> Optional[int]:
    sender = getattr(obj, "sender", None) or getattr(obj, "user", None)
    return getattr(sender, "user_id", None)


async def _guard(message) -> bool:
    if not _is_admin(_uid(message)):
        await message.answer(text="Эта команда только для администраторов.")
        return False
    return True


def _profiles_kb():
    kb = InlineKeyboardBuilder()
    for p in profiles.all_profiles():
        mark = "🟢" if p["enabled"] else "⏸"
        kb.callback(text=f"{mark} {p['id']} — {p['salon']}"[:60], payload=f"bo_show:{p['id']}")
    kb.adjust(1)
    return kb


def _profile_kb(p: dict):
    kb = InlineKeyboardBuilder()
    if p["enabled"]:
        kb.callback(text="⏸ Выключить", payload=f"bo_off:{p['id']}")
    else:
        kb.callback(text="▶️ Включить", payload=f"bo_on:{p['id']}")
    kb.callback(text="📊 Статистика", payload=f"bo_stats:{p['id']}")
    kb.callback(text="⬅️ Все профили", payload="bo_list")
    kb.adjust(1)
    return kb


def _stats_text(profile_id: Optional[str] = None) -> str:
    s = dialogs.stats(profile_id)
    return (f"Написали продавцам: {s['total']}\nОтветили: {s['replied']}\n"
            f"Готовы и переданы в чат: {s[dialogs.READY]} "
            f"(выкуп {s['ready_buyout']}, комиссия {s['ready_commission']})\n"
            f"Ждут менеджера: {s[dialogs.NEED_HUMAN]}\nВ диалоге: {s[dialogs.ACTIVE]}\n"
            f"Отказ: {s[dialogs.DECLINED]} · неактуально: {s[dialogs.NOT_RELEVANT]}")


def _list_text() -> str:
    items = profiles.all_profiles()
    head = "🚗 Выкуп и комиссия — ИИ-агент\n\n"
    if not items:
        return head + ("Профилей пока нет. Создайте первый:\n/buyout_new tmn-toyota Тойота Центр Тюмень\n\n" + HELP)
    return head + _stats_text() + "\n\nПрофили (нажмите, чтобы открыть):"


@router.message(Command("buyout"))
async def cmd_buyout(message, bot):
    if not await _guard(message):
        return
    await message.answer(text=_list_text(), keyboard=_profiles_kb())


@router.message(Command("buyout_help"))
async def cmd_buyout_help(message, bot):
    if await _guard(message):
        await message.answer(text=HELP)


@router.message(Command("buyout_new"))
async def cmd_buyout_new(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(text="Использование: /buyout_new <id> <Название салона>\n"
                                  "Например: /buyout_new tmn-toyota Тойота Центр Тюмень")
        return
    try:
        p = profiles.create(parts[0].lower(), parts[1].strip())
    except ValueError as e:
        await message.answer(text=str(e))
        return
    await message.answer(text="Профиль создан.\n\n" + profiles.describe(p) + "\n\n" + HELP)


@router.message(Command("buyout_set"))
async def cmd_buyout_set(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(text="Использование: /buyout_set <id> ключ=значение; ключ=значение\n\n" + HELP)
        return
    try:
        p = profiles.set_values(parts[0].lower(), parts[1])
    except ValueError as e:
        await message.answer(text=f"⚠️ {e}")
        return
    await message.answer(text="Сохранено.\n\n" + profiles.describe(p), keyboard=_profile_kb(p))


@router.message(Command("buyout_show"))
async def cmd_buyout_show(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    p = profiles.get((command.args or "").strip().lower())
    if not p:
        await message.answer(text="Профиль не найден. Список: /buyout")
        return
    await message.answer(text=profiles.describe(p), keyboard=_profile_kb(p))


@router.message(Command("buyout_chat"))
async def cmd_buyout_chat(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    profile_id = (command.args or "").strip().lower()
    try:
        p = profiles.set_chat(profile_id, message.recipient.chat_id)
    except ValueError as e:
        await message.answer(text=f"{e}\nИспользование (в рабочем чате): /buyout_chat <id профиля>")
        return
    await message.answer(text=f"Готово. Клиенты, готовые к выкупу по профилю «{p['salon']}», будут приходить в этот чат.")


async def _toggle(answer, profile_id: str, enabled: bool):
    try:
        p = profiles.get(profile_id)
        if not p:
            raise ValueError("Профиль не найден")
        if enabled and profiles.problems(p):
            await answer(text="Нельзя включить — не хватает настроек:\n• " + "\n• ".join(profiles.problems(p)))
            return
        p = profiles.set_enabled(profile_id, enabled)
    except ValueError as e:
        await answer(text=str(e))
        return
    state = "🟢 Агент запущен — начнёт писать продавцам в рабочие часы." if enabled else "⏸ Агент остановлен."
    await answer(text=f"{state}\n\n" + profiles.describe(p), keyboard=_profile_kb(p))


@router.message(Command("buyout_on"))
async def cmd_buyout_on(message, command: CommandObject, bot):
    if await _guard(message):
        await _toggle(message.answer, (command.args or "").strip().lower(), True)


@router.message(Command("buyout_off"))
async def cmd_buyout_off(message, command: CommandObject, bot):
    if await _guard(message):
        await _toggle(message.answer, (command.args or "").strip().lower(), False)


@router.message(Command("buyout_del"))
async def cmd_buyout_del(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    ok = profiles.delete((command.args or "").strip().lower())
    await message.answer(text="Профиль удалён." if ok else "Профиль не найден.")


@router.message(Command("buyout_mute"))
async def cmd_buyout_mute(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    key = (command.args or "").strip()
    if not dialogs.get(key):
        await message.answer(text="Диалог не найден. ID диалога указан внизу карточки клиента.")
        return
    dialogs.update(key, status=dialogs.NEED_HUMAN)
    await message.answer(text="Агент больше не отвечает в этом диалоге — новые сообщения клиента будут пересылаться сюда.")


@router.message(Command("buyout_stats"))
async def cmd_buyout_stats(message, command: CommandObject, bot):
    if await _guard(message):
        pid = (command.args or "").strip().lower() or None
        await message.answer(text=("Все профили" if not pid else f"Профиль {pid}") + ":\n" + _stats_text(pid))


# --- кнопки ---------------------------------------------------------------

async def _cb_guard(callback) -> bool:
    if not _is_admin(_uid(callback)):
        try:
            await callback.answer(notification="Только для администраторов.", clear_attachments=False)
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


@router.message_callback(F.payload == "bo_list")
async def on_bo_list(callback, bot):
    if await _cb_guard(callback):
        await callback.answer(text=_list_text(), keyboard=_profiles_kb())


@router.message_callback(F.payload.startswith("bo_show:"))
async def on_bo_show(callback, bot):
    if not await _cb_guard(callback):
        return
    p = profiles.get(callback.payload.split(":", 1)[1])
    if p:
        await callback.answer(text=profiles.describe(p), keyboard=_profile_kb(p))


@router.message_callback(F.payload.startswith("bo_on:"))
async def on_bo_on(callback, bot):
    if await _cb_guard(callback):
        await _toggle(callback.answer, callback.payload.split(":", 1)[1], True)


@router.message_callback(F.payload.startswith("bo_off:"))
async def on_bo_off(callback, bot):
    if await _cb_guard(callback):
        await _toggle(callback.answer, callback.payload.split(":", 1)[1], False)


@router.message_callback(F.payload.startswith("bo_stats:"))
async def on_bo_stats(callback, bot):
    if not await _cb_guard(callback):
        return
    pid = callback.payload.split(":", 1)[1]
    p = profiles.get(pid)
    await callback.answer(text=f"Профиль {pid}:\n" + _stats_text(pid), keyboard=_profile_kb(p) if p else None)


# --- песочница: проверить, как агент общается -----------------------------

def _sample_listing(profile: dict) -> Listing:
    f = profile["filters"]
    brand = (f["brands"] or ["Toyota"])[0]
    model = (f["models"] or ["Camry"])[0]
    year = f["year_to"] or f["year_from"] or 2019
    price = f["price_to"] or 2_300_000
    return Listing(platform="avito", listing_id="sandbox", url="https://www.avito.ru/...",
                   title=f"{brand} {model}, {year}", brand=brand, model=model, year=year,
                   price=price, mileage=87_000, owners=2, city="", is_private=True,
                   description="Машина в хорошем состоянии, обслуживалась у дилера. Торг у капота.")


@router.message(Command("buyout_try"))
async def cmd_buyout_try(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    args = (command.args or "").split()
    p = profiles.get(args[0].lower()) if args else None
    if not p:
        await message.answer(text="Использование: /buyout_try <id профиля> [выкуп|комиссия]")
        return
    offer = p.get("offer", "buyout")
    if len(args) > 1:
        try:
            offer = profiles.parse_value("offer", args[1])
        except ValueError as e:
            await message.answer(text=str(e))
            return
    listing = _sample_listing(p)
    await message.answer(text=f"🧪 Песочница. Вы — продавец: {listing.summary()}.\n"
                              "Отвечайте агенту командой /t <текст>. Ничего никуда не отправляется.")
    try:
        text = await asyncio.to_thread(agent.opening_message, p, listing, offer)
    except Exception as e:  # noqa: BLE001
        await message.answer(text=f"Агент недоступен: {type(e).__name__}: {e}\nПроверьте ANTHROPIC_API_KEY в .env.")
        return
    _sandboxes[_uid(message)] = {"profile_id": p["id"], "dialog": {
        "key": "sandbox", "listing": listing.to_dict(), "lead": {}, "status": dialogs.ACTIVE, "offer": offer,
        "messages": [{"from": "us", "text": text}]}}
    await message.answer(text=f"🤖 {text}")


@router.message(Command("t"))
async def cmd_sandbox_reply(message, command: CommandObject, bot):
    if not await _guard(message):
        return
    box = _sandboxes.get(_uid(message))
    if not box:
        await message.answer(text="Сначала запустите песочницу: /buyout_try <id профиля>")
        return
    text = (command.args or "").strip()
    if not text:
        return
    p = profiles.get(box["profile_id"])
    dialog = box["dialog"]
    dialog["messages"].append({"from": "client", "text": text})
    try:
        result = await asyncio.to_thread(agent.next_turn, p, dialog)
    except Exception as e:  # noqa: BLE001
        await message.answer(text=f"Агент недоступен: {type(e).__name__}: {e}")
        return
    dialog["lead"] = {**dialog["lead"], **{k: v for k, v in result["lead"].items() if v and v != "unknown"}}
    dialog["status"] = result["status"]
    if result["reply"]:
        dialog["messages"].append({"from": "us", "text": result["reply"]})
        await message.answer(text=f"🤖 {result['reply']}")
    if result["status"] != dialogs.ACTIVE:
        from buyout.engine import lead_card
        await message.answer(text=f"[статус: {dialogs.STATUS_TITLES[result['status']]}]\n\n"
                                  "Так выглядела бы карточка в рабочем чате:\n\n" + lead_card(p, dialog))
        _sandboxes.pop(_uid(message), None)
