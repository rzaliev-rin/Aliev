# -*- coding: utf-8 -*-
"""
ИИ-агент отдела выкупа: пишет первое сообщение продавцу и ведёт диалог
живым, человеческим языком, пока клиент не будет готов к выкупу (или не откажется).

Каждый ход агент возвращает структурированный ответ:
  reply   — текст, который уходит продавцу (может быть пустым — молчим)
  status  — active / ready / declined / not_relevant / need_human
  lead    — что удалось узнать: имя, телефон, удобное время, состояние, ожидания по цене

Модель: Claude (Anthropic API). Ключ — ANTHROPIC_API_KEY в .env.
"""
import json
import logging
import os
from typing import Optional

import anthropic

from buyout import dialogs
from buyout.listings import Listing

logger = logging.getLogger(__name__)

MODEL = os.environ.get("BUYOUT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("BUYOUT_EFFORT", "medium")  # low / medium / high

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # ANTHROPIC_API_KEY из окружения
    return _client


# Общие правила — одинаковые для всех салонов (кэшируются на стороне API).
RULES = """Ты ведёшь переписку от имени отдела выкупа автомобилей автосалона с частными продавцами, \
которые разместили объявление о продаже машины на Авито, Авто.ру или Дроме. Салон хочет выкупить машину.

Как писать:
- Как живой менеджер в мессенджере: коротко (1–3 предложения), просто, вежливо, на «вы». \
Без списков, без канцелярита, без рекламных штампов и восклицательных знаков подряд. Эмодзи — максимум изредка.
- Подстраивайся под тон собеседника: отвечает коротко — и ты коротко.
- Один вопрос за раз. Не устраивай допрос и не вываливай все преимущества сразу.
- Не повторяй дословно то, что уже писал. Не начинай каждое сообщение с приветствия.
- Пиши без ошибок, но не идеально-официально: «добрый день», «подскажите», «понял», «хорошо» — нормально.

Цель: понять, готов ли продавец рассмотреть выкуп салоном, и договориться о звонке или осмотре. \
По ходу разговора ненавязчиво уточни то, чего нет в объявлении: состояние, ДТП/окрасы, \
сколько владельцев, ПТС (оригинал/электронный), нет ли кредита или залога, за сколько готов отдать, \
когда удобно показать машину. Всё узнавать не обязательно — главное контакт и готовность.

Когда клиент готов: попроси номер телефона и удобное время для звонка менеджера \
(или договорись о времени осмотра). Как только есть телефон и согласие на звонок/осмотр — \
поблагодари, скажи, что менеджер свяжется, и поставь status=ready.

Цена: конкретную сумму выкупа НЕ называй и не обещай — итоговую цену менеджер назовёт после осмотра \
(салон проверяет машину и делает предложение). Если продавец настаивает, можно сказать, что \
ориентируетесь на рынок и готовы предложить честную цену после осмотра, и спросить его ожидания.

Честность:
- Не выдумывай факты о салоне, ценах, акциях, адресах — используй только то, что указано в данных салона.
- Если спрашивают, бот ли ты / живой ли человек — не ври: скажи, что ты помощник отдела выкупа салона, \
и предложи, чтобы перезвонил менеджер. Сам эту тему не поднимай.
- Не давай юридических и финансовых гарантий.

Когда остановиться:
- Продавец отказался, попросил не писать, грубит — вежливо попрощайся одной фразой (или промолчи, \
если просил не писать) и поставь status=declined. Никогда не уговаривай после явного отказа.
- Машина уже продана, это перекупщик/салон, обмен вместо продажи, машина не та — status=not_relevant.
- Вопрос, на который ты не можешь ответить без менеджера (конкретная цена, trade-in на конкретную \
машину салона, юридические детали), и клиент настаивает — status=need_human, ответь, что уточнишь \
у менеджера и он свяжется.
- Иначе status=active.

reply — ровно тот текст, который уйдёт продавцу. Пустой reply — если отвечать не нужно \
(например, клиент просил больше не писать).
lead — что известно о клиенте и машине на этот момент (пустые строки, если неизвестно). \
phone — только цифры с кодом, например 79991234567. summary — 1–2 предложения для менеджера: \
что за клиент, на чём договорились."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "status": {"type": "string", "enum": [dialogs.ACTIVE, dialogs.READY, dialogs.DECLINED,
                                               dialogs.NOT_RELEVANT, dialogs.NEED_HUMAN]},
        "lead": {
            "type": "object",
            "properties": {
                "client_name": {"type": "string"},
                "phone": {"type": "string"},
                "best_time": {"type": "string"},
                "price_expectation": {"type": "string"},
                "condition": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["client_name", "phone", "best_time", "price_expectation", "condition", "summary"],
            "additionalProperties": False,
        },
    },
    "required": ["reply", "status", "lead"],
    "additionalProperties": False,
}


def salon_block(profile: dict) -> str:
    pitch = profile["pitch"]
    lines = [
        "Данные салона:",
        f"- Салон: {pitch.get('salon_title') or profile['salon']}",
        f"- Тебя зовут: {pitch.get('manager_name') or 'менеджер отдела выкупа'}",
    ]
    if pitch.get("address"):
        lines.append(f"- Адрес: {pitch['address']}")
    if pitch.get("advantages"):
        lines.append("- Что можно предложить продавцу: " + "; ".join(pitch["advantages"]))
    if pitch.get("extra"):
        lines.append(f"- Дополнительно: {pitch['extra']}")
    return "\n".join(lines)


def _listing_block(listing: Listing) -> str:
    lines = [f"Объявление ({listing.platform}): {listing.summary()}"]
    if listing.owners:
        lines.append(f"Владельцев: {listing.owners}")
    if listing.seller_name:
        lines.append(f"Продавец: {listing.seller_name}")
    if listing.description:
        lines.append("Текст объявления: " + listing.description[:1500])
    return "\n".join(lines)


def _transcript(messages: list) -> str:
    if not messages:
        return "(переписки ещё нет)"
    who = {"us": "Мы", "client": "Продавец", "manager": "Мы (менеджер)"}
    return "\n".join(f"{who.get(m['from'], m['from'])}: {m['text']}" for m in messages)


def _ask(profile: dict, task: str) -> dict:
    """Один запрос к модели. Всё состояние диалога передаём текстом — это просто и надёжно."""
    response = _get_client().beta.messages.create(
        model=MODEL,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # если основная модель откажется — запрос сам повторится на запасной
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        system=[
            {"type": "text", "text": RULES, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": salon_block(profile)},
        ],
        messages=[{"role": "user", "content": task}],
    )
    if response.stop_reason == "refusal":
        logger.warning("Модель отказалась отвечать: %s", response.stop_details)
        return {"reply": "", "status": dialogs.NEED_HUMAN,
                "lead": {"summary": "Агент не смог ответить — нужен менеджер"}}
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


def opening_message(profile: dict, listing: Listing) -> str:
    """Первое сообщение продавцу по объявлению."""
    task = (
        _listing_block(listing)
        + "\n\nНапиши ПЕРВОЕ сообщение этому продавцу. Представься по имени и салону, скажи, что "
        "увидели объявление и салон интересуется выкупом этой машины, и задай один простой вопрос "
        "(например, актуальна ли продажа и рассматривает ли выкуп салоном). Без длинных перечислений "
        "преимуществ — максимум одно. 1–3 предложения. status=active."
    )
    return _ask(profile, task)["reply"].strip()


def next_turn(profile: dict, dialog: dict) -> dict:
    """Следующий ход в диалоге после ответа продавца."""
    listing = Listing.from_dict(dialog["listing"])
    known = {k: v for k, v in (dialog.get("lead") or {}).items() if v}
    task = (
        _listing_block(listing)
        + "\n\nПереписка (по порядку):\n" + _transcript(dialog["messages"])
        + ("\n\nУже известно о клиенте: " + json.dumps(known, ensure_ascii=False) if known else "")
        + "\n\nНапиши следующий ответ продавцу на его последнее сообщение."
    )
    result = _ask(profile, task)
    result["reply"] = (result.get("reply") or "").strip()
    return result


def followup_message(profile: dict, dialog: dict) -> str:
    """Одно мягкое напоминание, если продавец не ответил на первое сообщение."""
    listing = Listing.from_dict(dialog["listing"])
    task = (
        _listing_block(listing)
        + "\n\nПереписка:\n" + _transcript(dialog["messages"])
        + "\n\nПродавец не ответил. Напиши одно короткое ненавязчивое напоминание (1 предложение), "
        "без давления, не повторяя первое сообщение. status=active."
    )
    return _ask(profile, task)["reply"].strip()
