# -*- coding: utf-8 -*-
"""Выкуп: фильтры, профили, движок (с ненастоящими площадкой и ИИ). Запуск: python tests/test_buyout.py"""
import asyncio
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
_tmp = tempfile.mkdtemp()
os.environ["BUYOUT_PROFILES_PATH"] = os.path.join(_tmp, "profiles.json")
os.environ["BUYOUT_DIALOGS_PATH"] = os.path.join(_tmp, "dialogs.json")

from buyout import agent, dialogs, engine, profiles  # noqa: E402
from buyout.listings import Listing, rejection_reason  # noqa: E402
from buyout.platforms import IncomingMessage, Platform, register  # noqa: E402
from buyout.platforms.browser import parse_params, parse_title  # noqa: E402

NOON = datetime.datetime.now().replace(hour=12)


def _reset():
    for p in (profiles.STORE_PATH, dialogs.STORE_PATH):
        if os.path.exists(p):
            os.remove(p)


def _profile():
    profiles.create("tmn", "Тойота Центр Тюмень")
    profiles.set_values("tmn", "brands=Toyota; year_from=2016; price_to=3 млн; mileage_max=150000; "
                               "manager_name=Алексей; avito_on=да; avito_url=https://avito.ru/s; avito_account=acc1")
    profiles.set_chat("tmn", -100)
    return profiles.set_enabled("tmn", True)


def _listing(i, **kw):
    base = dict(platform="avito", listing_id=str(i), url=f"https://avito.ru/{i}", title="Toyota Camry, 2019",
                brand="Toyota", model="Camry", year=2019, price=2_400_000, mileage=80_000 + i * 1000,
                is_private=True, seller_id=f"s{i}")
    base.update(kw)
    return Listing(**base)


class FakePlatform(Platform):
    name = "avito"

    def __init__(self, found=None):
        self.found = found or []
        self.sent = []
        self.inbox = []

    async def search(self, search_url, limit=50):
        return list(self.found)

    async def send(self, account, listing, text):
        self.sent.append((listing.key, text))

    async def incoming(self, account):
        out, self.inbox = self.inbox, []
        return out


def _fake_agent(script):
    """script: список ответов модели для next_turn по порядку."""
    agent.opening_message = lambda profile, listing, offer=None: (
        f"Добрый день! Я Алексей, {profile['salon']} ({offer}). Машина ещё продаётся?")
    agent.followup_message = lambda profile, dialog: "Подскажите, вопрос по выкупу ещё актуален?"
    it = iter(script)
    agent.next_turn = lambda profile, dialog: next(it)


def _turn(reply, status="active", **lead):
    full = {"deal_type": "unknown"}
    full.update({k: "" for k in ("client_name", "phone", "best_time", "price_expectation", "condition", "summary")})
    full.update(lead)
    return {"reply": reply, "status": status, "lead": full}


async def _no_sleep(_):
    return None


def _engine(notes):
    async def notify(chat_id, text):
        notes.append((chat_id, text))

    async def alert(text):
        notes.append(("alert", text))
    return engine.BuyoutEngine(notify, alert, sleep=_no_sleep)


def test_filters():
    f = profiles.DEFAULT_PROFILE["filters"] | {"brands": ["Toyota"], "year_from": 2016, "price_to": 3_000_000,
                                                "mileage_max": 150_000}
    assert rejection_reason(_listing(1), f) is None
    assert rejection_reason(_listing(1, brand="Kia", title="Kia Rio"), f) == "марка"
    assert rejection_reason(_listing(1, year=2012), f) == "год"
    assert rejection_reason(_listing(1, price=3_500_000), f) == "цена"
    assert rejection_reason(_listing(1, mileage=200_000), f) == "пробег"
    assert rejection_reason(_listing(1, is_private=False), f) == "не частник"
    assert rejection_reason(_listing(1, description="Битый перед"), f).startswith("стоп-слово")


def test_parsing():
    assert parse_title("Toyota Camry 2.5 AT, 2019") == {"year": 2019, "brand": "Toyota", "model": "Camry"}
    p = parse_params("2.5 AT (181 л.с.), седан, 87\xa0000 км, 2 владельца")
    assert p["mileage"] == 87000 and p["transmission"] == "автомат" and p["owners"] == 2


def test_profiles():
    _reset()
    p = profiles.create("tmn", "Тойота Центр Тюмень")
    assert profiles.problems(p)  # пока не настроен
    _reset()
    p = _profile()
    assert p["filters"]["price_to"] == 3_000_000 and p["filters"]["brands"] == ["Toyota"]
    assert profiles.problems(p) == []
    try:
        profiles.set_values("tmn", "nonsense=1")
        raise AssertionError("ожидалась ошибка")
    except ValueError:
        pass


def test_engine_full_dialog():
    _reset()
    profile = _profile()
    fake = FakePlatform(found=[_listing(1), _listing(2, year=2010), _listing(3)])
    register(fake)
    notes = []
    eng = _engine(notes)
    _fake_agent([
        _turn("Понял. Подскажите, по состоянию — были ДТП или окрасы?"),
        _turn("Отлично. Оставьте номер — менеджер наберёт и договоритесь об осмотре.",
              condition="без ДТП"),
        _turn("Спасибо, Сергей! Менеджер позвонит сегодня после 18.", status="ready", deal_type="buyout",
              client_name="Сергей", phone="79991234567", best_time="после 18", summary="Готов к осмотру"),
    ])
    asyncio.run(eng.run_profile(profile, now=NOON))
    assert [k for k, _ in fake.sent] == ["avito:1", "avito:3"]  # 2010 год отсеян
    assert dialogs.is_seen(_listing(2))

    # Второй круг: тех же продавцов не трогаем
    asyncio.run(eng.run_profile(profile, now=NOON))
    assert len(fake.sent) == 2

    for text in ("Да, продаю", "Нет, не битая", "Сергей, 8 999 123-45-67, после 18"):
        fake.inbox = [IncomingMessage("avito:1", [text])]
        asyncio.run(eng.run_profile(profile, now=NOON))
    d = dialogs.get("avito:1")
    assert d["status"] == dialogs.READY and d["handed_off"]
    assert d["lead"]["phone"] == "79991234567" and d["lead"]["condition"] == "без ДТП"
    cards = [t for c, t in notes if c == -100]
    assert len(cards) == 1 and "+7 999 123-45-67" in cards[0] and "Сергей" in cards[0]
    assert cards[0].startswith("🔥 Выкуп — клиент готов к разговору")

    # После передачи менеджеру агент молчит, сообщения клиента пересылаются в чат
    sent_before = len(fake.sent)
    fake.inbox = [IncomingMessage("avito:1", ["А во сколько точно?"])]
    asyncio.run(eng.run_profile(profile, now=NOON))
    assert len(fake.sent) == sent_before
    assert "А во сколько точно?" in notes[-1][1]


def test_limits_hours_and_followup():
    _reset()
    profile = _profile()
    profiles.set_values("tmn", "per_day=2")
    profile = profiles.get("tmn")
    fake = FakePlatform(found=[_listing(i) for i in range(1, 6)])
    register(fake)
    _fake_agent([])
    eng = _engine([])
    asyncio.run(eng.run_profile(profile, now=NOON.replace(hour=23)))
    assert fake.sent == []  # ночью не пишем
    asyncio.run(eng.run_profile(profile, now=NOON))
    assert len(fake.sent) == 2  # дневной лимит

    # Через сутки без ответа — одно напоминание, не больше
    data = json.load(open(dialogs.STORE_PATH, encoding="utf-8"))
    for d in data["dialogs"].values():
        d["created"] = (datetime.datetime.now() - datetime.timedelta(days=2)).isoformat(timespec="seconds")
    json.dump(data, open(dialogs.STORE_PATH, "w", encoding="utf-8"))
    asyncio.run(eng.followups(profile))
    asyncio.run(eng.followups(profile))
    followups = [t for _, t in fake.sent if "актуален" in t]
    assert len(followups) == 2  # по одному на каждый из двух диалогов


def test_commission():
    """Дорогие машины (выше лимита выкупа) — только на комиссию; ещё дороже — пропускаем."""
    _reset()
    _profile()
    profiles.set_values("tmn", "offer=оба; commission_price_to=8 млн; commission_terms=5%, договор 45 дней")
    profile = profiles.get("tmn")
    fake = FakePlatform(found=[_listing(1), _listing(2, price=5_500_000), _listing(3, price=9_000_000)])
    register(fake)
    notes = []
    _fake_agent([_turn("Отлично, тогда менеджер позвонит завтра в 11 и всё расскажет по договору.",
                       status="ready", deal_type="commission", phone="79990001122", best_time="завтра в 11")])
    eng = _engine(notes)
    asyncio.run(eng.run_profile(profile, now=NOON))
    assert [k for k, _ in fake.sent] == ["avito:1", "avito:2"]
    assert "(both)" in fake.sent[0][1] and "(commission)" in fake.sent[1][1]
    assert dialogs.get("avito:2")["offer"] == "commission"
    fake.inbox = [IncomingMessage("avito:2", ["Давайте на комиссию, 79990001122, завтра в 11"])]
    asyncio.run(eng.run_profile(profile, now=NOON))
    card = [t for c, t in notes if c == -100][0]
    assert card.startswith("🤝 Комиссия — клиент готов к разговору")
    s = dialogs.stats("tmn")
    assert s["ready_commission"] == 1 and s["ready_buyout"] == 0

    # Только выкуп: дорогую машину не трогаем
    _reset()
    _profile()
    profiles.set_values("tmn", "offer=выкуп; commission_price_to=8 млн")
    fake.sent.clear()
    asyncio.run(eng.run_profile(profiles.get("tmn"), now=NOON))
    assert [k for k, _ in fake.sent] == ["avito:1"]


def test_bitrix():
    """Готовый клиент → лид/сделка в Битрикс24, ссылка в чате; ответственные — по очереди."""
    import requests
    from buyout import bitrix
    calls = []

    class Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d

    def fake_post(url, json=None, timeout=None):
        calls.append((url.rsplit("/", 1)[1], json))
        return Resp({"result": 100 + len(calls)})

    requests.post = fake_post
    bitrix.STATE_PATH = os.path.join(_tmp, "bitrix_state.json")
    os.environ["BITRIX_WEBHOOK_URL"] = "https://expocar.bitrix24.ru/rest/1/secret/"
    os.environ["BITRIX_ENTITY"] = "lead"
    try:
        _reset()
        _profile()
        profiles.set_values("tmn", "bitrix_responsible=7,9")
        profile = profiles.get("tmn")
        fake = FakePlatform(found=[_listing(1), _listing(3)])
        register(fake)
        notes = []
        ready = _turn("Спасибо! Менеджер позвонит после 18.", status="ready", deal_type="buyout",
                      client_name="Сергей", phone="79991234567", best_time="после 18", summary="Готов к осмотру")
        _fake_agent([ready, dict(ready)])
        eng = _engine(notes)
        asyncio.run(eng.run_profile(profile, now=NOON))
        for key in ("avito:1", "avito:3"):
            fake.inbox = [IncomingMessage(key, ["Сергей, 89991234567, после 18"])]
            asyncio.run(eng.run_profile(profile, now=NOON))
        assert [c[0] for c in calls] == ["crm.lead.add.json", "crm.lead.add.json"]
        f = calls[0][1]["fields"]
        assert f["TITLE"].startswith("Выкуп: Toyota Camry") and f["NAME"] == "Сергей"
        assert f["PHONE"][0]["VALUE"] == "+79991234567" and "Переписка" in f["COMMENTS"]
        assert [c[1]["fields"]["ASSIGNED_BY_ID"] for c in calls] == [7, 9]  # по очереди
        card = [t for c, t in notes if c == -100][0]
        assert "📋 Битрикс24: https://expocar.bitrix24.ru/crm/lead/details/101/" in card
        assert "👤 Клиент: Сергей, +7 999 123-45-67" in card and "Последние сообщения" not in card

        # Сделка: сначала контакт, потом сделка в воронке профиля
        calls.clear()
        _reset()
        _profile()
        profiles.set_values("tmn", "bitrix_entity=сделка; bitrix_category=3")
        register(FakePlatform(found=[_listing(5)]))
        fake = FakePlatform(found=[_listing(5)])
        register(fake)
        _fake_agent([dict(ready)])
        asyncio.run(eng.run_profile(profiles.get("tmn"), now=NOON))
        fake.inbox = [IncomingMessage("avito:5", ["да"])]
        asyncio.run(eng.run_profile(profiles.get("tmn"), now=NOON))
        assert [c[0] for c in calls] == ["crm.contact.add.json", "crm.deal.add.json"]
        assert calls[1][1]["fields"]["CONTACT_ID"] == 101 and calls[1][1]["fields"]["CATEGORY_ID"] == 3
        assert dialogs.get("avito:5")["bitrix_url"].endswith("/crm/deal/details/102/")

        # Битрикс24 недоступен — клиент всё равно приходит в чат, с пометкой
        requests.post = lambda *a, **k: Resp({"error": "INVALID_CREDENTIALS", "error_description": "Неверный вебхук"})
        _reset()
        _profile()
        fake = FakePlatform(found=[_listing(6)])
        register(fake)
        _fake_agent([dict(ready)])
        notes.clear()
        asyncio.run(eng.run_profile(profiles.get("tmn"), now=NOON))
        fake.inbox = [IncomingMessage("avito:6", ["да"])]
        asyncio.run(eng.run_profile(profiles.get("tmn"), now=NOON))
        card = [t for c, t in notes if c == -100][0]
        assert "⚠️ Карточка в Битрикс24 не создана: Битрикс24: Неверный вебхук" in card
        assert any(c == "alert" for c, _ in notes)
    finally:
        os.environ.pop("BITRIX_WEBHOOK_URL", None)


def test_decline_stops():
    _reset()
    profile = _profile()
    fake = FakePlatform(found=[_listing(1)])
    register(fake)
    _fake_agent([_turn("Понял, извините за беспокойство. Удачной продажи!", status="declined")])
    eng = _engine([])
    asyncio.run(eng.run_profile(profile, now=NOON))
    fake.inbox = [IncomingMessage("avito:1", ["Не интересно, не пишите"])]
    asyncio.run(eng.run_profile(profile, now=NOON))
    assert dialogs.get("avito:1")["status"] == dialogs.DECLINED
    data = json.load(open(dialogs.STORE_PATH, encoding="utf-8"))
    for d in data["dialogs"].values():
        d["created"] = "2020-01-01T00:00:00"
    json.dump(data, open(dialogs.STORE_PATH, "w", encoding="utf-8"))
    n = len(fake.sent)
    asyncio.run(eng.followups(profile))
    assert len(fake.sent) == n  # отказавшимся не напоминаем


def test_agent_request_shape():
    """Запрос к Claude собирается правильно (без сети)."""
    import importlib
    import anthropic
    import httpx2 as httpx
    importlib.reload(agent)
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        body = {"id": "msg_1", "type": "message", "role": "assistant", "model": agent.MODEL,
                "content": [{"type": "text", "text": json.dumps(_turn("Добрый день! Машина ещё в продаже?"),
                                                                ensure_ascii=False)}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10}}
        return httpx.Response(200, json=body)

    agent._client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    _reset()
    profile = _profile()
    text = agent.opening_message(profile, _listing(1))
    assert text == "Добрый день! Машина ещё в продаже?"
    assert captured["fallbacks"] == "default"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert "Алексей" in captured["system"][1]["text"]
    assert "Toyota Camry" in captured["messages"][0]["content"]
    assert "Что предлагаем: выкуп или комиссию" in captured["messages"][0]["content"]
    assert "Плюсы комиссии" in captured["system"][1]["text"]
    assert "commission" in captured["output_config"]["format"]["schema"]["properties"]["lead"]["properties"]["deal_type"]["enum"]
    agent._client = None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("OK", name)
