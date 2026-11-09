# -*- coding: utf-8 -*-
"""
Движок выкупа и комиссии: раз в несколько минут для каждого включённого профиля
  1) забирает новые ответы продавцов и отвечает им через ИИ-агента;
  2) ищет новые объявления по фильтрам и пишет первое сообщение (с дневным лимитом);
  3) раз в сутки мягко напоминает тем, кто не ответил (один раз);
  4) готовых к выкупу / комиссии клиентов отправляет карточкой в рабочий чат салона.

С ботом MAX связан двумя функциями: notify(chat_id, text) и alert(text).
"""
import asyncio
import datetime
import logging
import os
import random
from typing import Awaitable, Callable, Optional

from buyout import agent, dialogs, profiles
from buyout.listings import Listing, rejection_reason
from buyout.platforms import PlatformError, get_platform

logger = logging.getLogger(__name__)

INTERVAL_MIN = int(os.environ.get("BUYOUT_INTERVAL_MIN", "10") or 10)
FOLLOWUP_AFTER_HOURS = int(os.environ.get("BUYOUT_FOLLOWUP_HOURS", "24") or 24)

Notify = Callable[[int, str], Awaitable[None]]
Alert = Callable[[str], Awaitable[None]]


def in_working_hours(hours: str, now: Optional[datetime.datetime] = None) -> bool:
    try:
        start, end = (int(x) for x in str(hours).split("-"))
    except ValueError:
        start, end = 9, 21
    h = (now or datetime.datetime.now()).hour
    return start <= h < end


CARD_TITLES = {
    "buyout": "🔥 Клиент готов к выкупу",
    "commission": "🤝 Клиент готов продать на комиссии",
    "unknown": "🔥 Клиент готов к разговору (выкуп или комиссия — уточнить)",
}


def choose_offer(profile: dict, listing: Listing) -> Optional[str]:
    """Что предложить по объявлению: buyout / commission / both, или None — не подходит.
    Машины дороже лимита выкупа (price_to), но до commission_price_to — только на комиссию."""
    offer = profile.get("offer", "buyout")
    filters = profile["filters"]
    if rejection_reason(listing, filters) is None:
        return offer
    limit = filters.get("commission_price_to")
    if (offer == "both" and limit and listing.price and filters.get("price_to")
            and listing.price > filters["price_to"]
            and rejection_reason(listing, {**filters, "price_to": limit}) is None):
        return "commission"
    return None


def lead_card(profile: dict, dialog: dict, title: Optional[str] = None) -> str:
    listing = Listing.from_dict(dialog["listing"])
    lead = dialog.get("lead") or {}
    lines = [
        title or CARD_TITLES[dialogs.deal_type(dialog)],
        f"Салон: {profile['salon']}",
        "",
        f"🚗 {listing.summary()}",
        listing.url,
        "",
    ]
    for label, key in (("Имя", "client_name"), ("📞 Телефон", "phone"), ("Когда удобно", "best_time"),
                       ("Ожидания по цене", "price_expectation"), ("Состояние", "condition")):
        if lead.get(key):
            value = lead[key]
            if key == "phone" and value.isdigit() and len(value) == 11:
                value = f"+{value[0]} {value[1:4]} {value[4:7]}-{value[7:9]}-{value[9:]}"
            lines.append(f"{label}: {value}")
    if lead.get("summary"):
        lines += ["", f"📝 {lead['summary']}"]
    tail = dialog["messages"][-6:]
    if tail:
        who = {"us": "Мы", "client": "Клиент", "manager": "Менеджер"}
        lines += ["", "Последние сообщения:"] + [f"{who.get(m['from'], m['from'])}: {m['text']}" for m in tail]
    lines += ["", f"ID диалога: {dialog['key']}"]
    return "\n".join(lines)


class BuyoutEngine:
    def __init__(self, notify: Notify, alert: Alert, sleep=asyncio.sleep):
        self.notify = notify
        self.alert = alert
        self.sleep = sleep            # подменяется в тестах, чтобы не ждать паузы
        self._alerted = {}

    async def _alert_once(self, key: str, text: str) -> None:
        """Одну и ту же проблему сообщаем не чаще раза в 3 часа."""
        now = datetime.datetime.now()
        last = self._alerted.get(key)
        if last and (now - last).total_seconds() < 3 * 3600:
            return
        self._alerted[key] = now
        await self.alert(text)

    async def _human_pause(self, bounds) -> None:
        a, b = (bounds or [60, 240])[:2]
        await self.sleep(random.uniform(a, b))

    # --- один проход --------------------------------------------------------

    async def tick(self, now: Optional[datetime.datetime] = None) -> None:
        tasks = [self.run_profile(p, now) for p in profiles.all_profiles() if p["enabled"]]
        if tasks:
            await asyncio.gather(*tasks)

    async def run_profile(self, profile: dict, now: Optional[datetime.datetime] = None) -> None:
        if profiles.problems(profile):
            return
        if not in_working_hours(profile["limits"]["hours"], now):
            return  # ночью не пишем: сообщения дождутся утра
        for platform_name, cfg in profile["platforms"].items():
            if not cfg.get("enabled"):
                continue
            try:
                platform = get_platform(platform_name)
                await self.handle_incoming(profile, platform, cfg["account"])
                await self.search_and_contact(profile, platform, cfg)
            except PlatformError as e:
                await self._alert_once(f"{profile['id']}:{platform_name}",
                                       f"Выкуп, профиль {profile['id']}: {e}")
            except Exception as e:  # noqa: BLE001 — один сбой не должен останавливать остальные профили
                logger.exception("Выкуп: ошибка профиля %s/%s", profile["id"], platform_name)
                await self._alert_once(f"{profile['id']}:{platform_name}:{type(e).__name__}",
                                       f"Выкуп, профиль {profile['id']} ({platform_name}): {type(e).__name__}: {e}")
        await self.followups(profile)

    # --- ответы продавцов ---------------------------------------------------

    async def handle_incoming(self, profile: dict, platform, account: str) -> None:
        for msg in await platform.incoming(account):
            dialog = dialogs.get(msg.listing_key)
            if not dialog or dialog["profile_id"] != profile["id"]:
                continue  # чат не наш (например, покупатель по объявлению самого салона)
            known = {m["text"] for m in dialog["messages"] if m["from"] == "client"}
            new_texts = [t for t in msg.texts if t not in known]
            if not new_texts:
                continue
            for text in new_texts:
                dialog = dialogs.add_message(dialog["key"], "client", text)
            await self.on_client_message(profile, platform, dialog)

    async def on_client_message(self, profile: dict, platform, dialog: dict) -> None:
        status = dialog["status"]
        last = dialog["messages"][-1]["text"]
        if status in (dialogs.READY, dialogs.NEED_HUMAN):
            # Клиентом уже занимается менеджер — не вмешиваемся, просто пересылаем.
            await self.notify(profile["chat_id"],
                              f"💬 Новое сообщение от клиента ({Listing.from_dict(dialog['listing']).summary()}):\n"
                              f"{last}\n\nID диалога: {dialog['key']}")
            return
        if status in (dialogs.DECLINED, dialogs.NOT_RELEVANT):
            dialog = dialogs.update(dialog["key"], status=dialogs.ACTIVE)  # передумал — продолжаем разговор

        result = await asyncio.to_thread(agent.next_turn, profile, dialog)
        lead = {**(dialog.get("lead") or {}),
                **{k: v for k, v in (result.get("lead") or {}).items() if v and v != "unknown"}}
        dialog = dialogs.update(dialog["key"], lead=lead, status=result["status"])
        if result["reply"]:
            await self._human_pause(profile["limits"].get("reply_delay_sec"))
            listing = Listing.from_dict(dialog["listing"])
            await platform.send(dialog["account"], listing, result["reply"])
            dialog = dialogs.add_message(dialog["key"], "us", result["reply"])
        if result["status"] == dialogs.READY and not dialog.get("handed_off"):
            await self.notify(profile["chat_id"], lead_card(profile, dialog))
            dialogs.update(dialog["key"], handed_off=True)
        elif result["status"] == dialogs.NEED_HUMAN:
            await self.notify(profile["chat_id"],
                              lead_card(profile, dialog, title="🙋 Нужен менеджер: агент не может ответить сам"))

    # --- новые объявления ----------------------------------------------------

    async def search_and_contact(self, profile: dict, platform, cfg: dict) -> int:
        account = cfg["account"]
        quota = profile["limits"]["first_messages_per_day"] - dialogs.first_messages_today(account)
        if quota <= 0:
            return 0
        sent = 0
        for listing in await platform.search(cfg["search_url"]):
            if sent >= quota:
                break
            if dialogs.is_seen(listing) or dialogs.already_contacted(listing):
                continue
            offer = choose_offer(profile, listing)
            if offer is not None:
                listing = await platform.details(listing)
                offer = choose_offer(profile, listing)  # описание/тип продавца — со страницы
            if offer is None:
                dialogs.mark_seen(listing, rejection_reason(listing, profile["filters"]) or "фильтр")
                continue
            text = await asyncio.to_thread(agent.opening_message, profile, listing, offer)
            if not text:
                dialogs.mark_seen(listing, "агент не составил сообщение")
                continue
            if sent:
                await self._human_pause(profile["limits"].get("first_delay_sec"))
            await platform.send(account, listing, text)
            dialogs.start(profile["id"], listing, text, account, offer)
            sent += 1
        return sent

    # --- напоминания ---------------------------------------------------------

    async def followups(self, profile: dict) -> None:
        cutoff = datetime.datetime.now() - datetime.timedelta(hours=FOLLOWUP_AFTER_HOURS)
        for dialog in dialogs.all_dialogs(profile["id"]):
            if dialog["status"] != dialogs.ACTIVE or dialog.get("followups", 0) >= 1:
                continue
            if any(m["from"] == "client" for m in dialog["messages"]):
                continue
            if datetime.datetime.fromisoformat(dialog["created"]) > cutoff:
                continue
            platform_name = dialog["listing"]["platform"]
            if not profile["platforms"].get(platform_name, {}).get("enabled"):
                continue
            try:
                text = await asyncio.to_thread(agent.followup_message, profile, dialog)
                if text:
                    await get_platform(platform_name).send(
                        dialog["account"], Listing.from_dict(dialog["listing"]), text)
                    dialogs.add_message(dialog["key"], "us", text)
                dialogs.update(dialog["key"], followups=1)
            except PlatformError as e:
                await self._alert_once(f"{profile['id']}:{platform_name}", f"Выкуп, профиль {profile['id']}: {e}")
                return


async def run_forever(engine: BuyoutEngine) -> None:
    while True:
        try:
            await engine.tick()
        except Exception:  # noqa: BLE001
            logger.exception("Выкуп: сбой цикла")
        await asyncio.sleep(INTERVAL_MIN * 60)
