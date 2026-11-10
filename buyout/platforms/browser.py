# -*- coding: utf-8 -*-
"""
Работа с Авито / Авто.ру / Дром через настоящий браузер (Playwright).

У площадок нет открытого API, чтобы написать чужому продавцу, поэтому бот
действует как обычный пользователь: открывает поиск, заходит в объявление,
жмёт «Написать», печатает текст. Вход в аккаунт делается один раз вручную
(python -m buyout.login avito <аккаунт>), сессия сохраняется в файл.

ВАЖНО: селекторы (на что нажимать на странице) у площадок меняются. Они вынесены
в SELECTORS и могут переопределяться файлом buyout_selectors.json без правки кода.
Перед запуском их нужно сверить на живом аккаунте (см. README, раздел «Выкуп»).
"""
import asyncio
import json
import logging
import os
import random
import re
from typing import Dict, List, Optional

from buyout.listings import Listing
from buyout.platforms.base import IncomingMessage, Platform, PlatformError

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
SESSIONS_DIR = os.environ.get("BUYOUT_SESSIONS_DIR", os.path.join(ROOT, "buyout_sessions"))
SELECTORS_OVERRIDE = os.path.join(ROOT, "buyout_selectors.json")
HEADLESS = os.environ.get("BUYOUT_HEADLESS", "1") != "0"

SELECTORS: Dict[str, dict] = {
    "avito": {
        "base": "https://www.avito.ru",
        "card": '[data-marker="item"]',
        "card_id_attr": "data-item-id",
        "card_link": 'a[data-marker="item-title"]',
        "card_title": '[data-marker="item-title"]',
        "card_price": '[itemprop="price"]',
        "card_price_attr": "content",
        "card_params": '[data-marker="item-specific-params"]',
        "card_seller": '[data-marker="item-line"]',
        "item_description": '[data-marker="item-view/item-description"]',
        "item_params": '[data-marker="item-view/item-params"]',
        "item_seller_type": '[data-marker="seller-info/label"]',
        "write_button": '[data-marker="messenger-button/button"]',
        "message_input": 'textarea[data-marker*="messenger"], [data-marker="reply/input"] textarea, textarea',
        "send_button": '[data-marker="reply/send"]',
        "inbox_url": "https://www.avito.ru/profile/messenger",
        "inbox_unread": '[data-marker="channels/channel"][data-unread="true"], [data-marker="channels/channel-unread"]',
        "chat_item_link": 'a[data-marker="channel-item/item-link"], a[href*="/items/"], a[href*="_"]',
        "chat_incoming": '[data-marker="message"][data-direction="in"], [data-marker*="message-in"]',
        "login_marker": '[data-marker="header/login-button"]',
    },
    "autoru": {
        "base": "https://auto.ru",
        "card": ".ListingItem",
        "card_id_attr": "",
        "card_link": "a.ListingItemTitle__link, a.Link.ListingItemTitle__link",
        "card_title": ".ListingItemTitle__link",
        "card_price": ".ListingItemPrice__content",
        "card_price_attr": "",
        "card_params": ".ListingItemTechSummaryDesktop, .ListingItem__techSummary",
        "card_seller": ".ListingItem__place, .MetroListPlace",
        "item_description": ".CardDescriptionHTML, .CardDescription__text",
        "item_params": ".CardInfo, .CardInfoRow",
        "item_seller_type": ".CardSellerNamePlace, .CardOwner",
        "write_button": ".OfferPhone__chat, button:has-text('Написать'), .PersonalMessage",
        "message_input": ".ChatInput textarea, textarea",
        "send_button": ".ChatInput__send, button:has-text('Отправить')",
        "inbox_url": "https://auto.ru/chat/",
        "inbox_unread": ".ChatListItem_unread, .ChatRoom_unread",
        "chat_item_link": "a[href*='/cars/used/sale/']",
        "chat_incoming": ".ChatMessage_type_incoming .ChatMessage__text, .ChatMessage_incoming",
        "login_marker": ".HeaderUserMenu__loginButton, a[href*='passport']",
    },
    "drom": {
        "base": "https://auto.drom.ru",
        "card": '[data-ftid="bulls-list_bull"]',
        "card_id_attr": "",
        "card_link": 'a[data-ftid="bull_title"], a[href*=".drom.ru/"]',
        "card_title": '[data-ftid="bull_title"]',
        "card_price": '[data-ftid="bull_price"]',
        "card_price_attr": "",
        "card_params": '[data-ftid="component_inline-bull-description"]',
        "card_seller": '[data-ftid="bull_location"]',
        "card_private": '[data-ftid="bull_label_owner"]',   # «от собственника» (проверено 09.11.2026)
        "item_description": "",
        "item_description_marker": "Дополнительно:",         # описание продавца идёт после этой фразы
        "item_params": "table",                               # Пробег / Владельцы / Коробка передач
        "item_seller_type": "",
        "write_button": 'button:has-text("Написать"), a:has-text("Написать")',
        "message_input": "textarea",
        "send_button": 'button:has-text("Отправить")',
        "inbox_url": "https://my.drom.ru/personal/messaging",
        "inbox_unread": ".dialog-unread, [data-unread='true']",
        "chat_item_link": "a[href*='.drom.ru/']",
        "chat_incoming": ".message-in, [data-direction='in']",
        "login_marker": 'a[href*="sign"], a:has-text("Вход")',
    },
}


def _selectors(platform: str) -> dict:
    sel = dict(SELECTORS[platform])
    if os.path.exists(SELECTORS_OVERRIDE):
        try:
            with open(SELECTORS_OVERRIDE, "r", encoding="utf-8") as f:
                sel.update(json.load(f).get(platform, {}))
        except Exception as e:  # noqa: BLE001
            logger.warning("buyout_selectors.json не прочитан: %s", e)
    return sel


def session_path(platform: str, account: str) -> str:
    return os.path.join(SESSIONS_DIR, platform, f"{account}.json")


# --- разбор текста карточки --------------------------------------------------

def _int(text: str) -> Optional[int]:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def parse_title(title: str) -> dict:
    """«Toyota Camry 2.5 AT, 2019» → марка, модель, год."""
    out = {}
    m = re.search(r"(19[8-9]\d|20[0-4]\d)", title or "")
    if m:
        out["year"] = int(m.group(1))
    name = re.split(r",|\s\d", title or "", maxsplit=1)[0].strip()
    words = name.split()
    if words:
        out["brand"] = words[0]
        out["model"] = " ".join(words[1:3]) if len(words) > 1 else ""
    return out


def parse_params(text: str) -> dict:
    out = {}
    t = (text or "").replace("\xa0", " ").replace(" ", " ")
    m = re.search(r"(\d[\d ]{0,8})\s*км", t)
    if m:
        out["mileage"] = _int(m.group(1))
    low = t.lower()
    for word, value in (("автомат", "автомат"), ("акпп", "автомат"), (" at", "автомат"),
                        ("механ", "механика"), ("мкпп", "механика"), (" mt", "механика"),
                        ("робот", "робот"), (" amt", "робот"), ("вариатор", "вариатор"), (" cvt", "вариатор")):
        if word in f" {low}":
            out["transmission"] = value
            break
    m = re.search(r"владельц\w*\D{0,10}(\d+)|(\d+)\s*владел", low)
    if m:
        out["owners"] = int(m.group(1) or m.group(2))
    return out


# --- сам браузер ------------------------------------------------------------

class BrowserPlatform(Platform):
    """Одна площадка; по контексту браузера на каждый аккаунт (+ анонимный для поиска)."""

    def __init__(self, name: str):
        self.name = name
        self.sel = _selectors(name)
        self._pw = None
        self._browser = None
        self._contexts: Dict[str, object] = {}

    async def _context(self, account: Optional[str]):
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise PlatformError("Не установлен Playwright: pip install playwright && playwright install chromium") from e
            self._pw = await async_playwright().start()
            exe = os.environ.get("BUYOUT_CHROMIUM_PATH") or None  # свой Chromium, если не через playwright install
            self._browser = await self._pw.chromium.launch(headless=HEADLESS, executable_path=exe)
        key = account or ""
        if key not in self._contexts:
            kwargs = {"locale": "ru-RU", "timezone_id": os.environ.get("BUYOUT_TZ", "Europe/Moscow"),
                      "viewport": {"width": 1366, "height": 860}}
            if account:
                path = session_path(self.name, account)
                if not os.path.exists(path):
                    raise PlatformError(f"Нет сохранённого входа для {self.name}/{account}. "
                                        f"Выполните: python -m buyout.login {self.name} {account}")
                kwargs["storage_state"] = path
            self._contexts[key] = await self._browser.new_context(**kwargs)
        return self._contexts[key]

    async def _save_session(self, account: str) -> None:
        ctx = self._contexts.get(account)
        if ctx is not None:
            await ctx.storage_state(path=session_path(self.name, account))

    @staticmethod
    async def _pause(a: float = 0.8, b: float = 2.5) -> None:
        await asyncio.sleep(random.uniform(a, b))

    async def _check_blocked(self, page) -> None:
        # Смотрим только видимый текст: слово captcha бывает в скриптах любой обычной страницы
        try:
            text = ((await page.title()) + " " + (await page.inner_text("body"))[:3000]).lower()
        except Exception:  # noqa: BLE001
            return
        if any(w in text for w in ("captcha", "я не робот", "вы не робот", "доступ ограничен",
                                   "доступ временно ограничен", "подозрительн")) or \
                await page.locator("iframe[src*='captcha'], [class*='captcha' i] input").count():
            raise PlatformError(f"{self.name}: площадка показала капчу/блокировку — нужна ручная проверка аккаунта")

    async def search(self, search_url: str, limit: int = 50) -> List[Listing]:
        s = self.sel
        ctx = await self._context(None)
        page = await ctx.new_page()
        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            await self._pause(2, 4)
            await self._check_blocked(page)
            cards = await page.query_selector_all(s["card"])
            out: List[Listing] = []
            for card in cards[:limit]:
                link_el = await card.query_selector(s["card_link"])
                href = (await link_el.get_attribute("href")) if link_el else ""
                if not href:
                    continue
                url = href if href.startswith("http") else s["base"] + href
                listing_id = (await card.get_attribute(s["card_id_attr"])) if s.get("card_id_attr") else None
                if not listing_id:
                    m = re.search(r"(\d{6,})(?:/|\.html|$|\?)", url)
                    listing_id = m.group(1) if m else url
                title_el = await card.query_selector(s["card_title"])
                title = (await title_el.inner_text()).strip() if title_el else ""
                price_el = await card.query_selector(s["card_price"])
                price = None
                if price_el:
                    raw = (await price_el.get_attribute(s["card_price_attr"])) if s.get("card_price_attr") else None
                    price = _int(raw or await price_el.inner_text())
                params_el = await card.query_selector(s["card_params"])
                params_text = (await params_el.inner_text()) if params_el else ""
                seller_el = await card.query_selector(s["card_seller"])
                seller_text = (await seller_el.inner_text()) if seller_el else ""
                is_private = None
                if s.get("card_private") and await card.query_selector(s["card_private"]):
                    is_private = True
                info = {**parse_title(title), **parse_params(f"{title} {params_text}")}
                out.append(Listing(platform=self.name, listing_id=str(listing_id), url=url, title=title,
                                   price=price, description=params_text, city=seller_text.split("\n")[-1][:60],
                                   is_private=is_private, **info))
            return out
        finally:
            await page.close()

    async def details(self, listing: Listing) -> Listing:
        s = self.sel
        ctx = await self._context(None)
        page = await ctx.new_page()
        try:
            await page.goto(listing.url, wait_until="domcontentloaded", timeout=60000)
            await self._pause(2, 5)
            await self._check_blocked(page)
            desc_el = await page.query_selector(s["item_description"]) if s.get("item_description") else None
            if desc_el:
                listing.description = (await desc_el.inner_text()).strip()[:3000]
            elif s.get("item_description_marker"):
                body = await page.inner_text("body")
                pos = body.find(s["item_description_marker"])
                if pos >= 0:
                    listing.description = body[pos + len(s["item_description_marker"]):].strip()[:1500]
            params_el = await page.query_selector(s["item_params"])
            if params_el:
                info = parse_params(await params_el.inner_text())
                for k, v in info.items():
                    if getattr(listing, k, None) in (None, ""):
                        setattr(listing, k, v)
            seller_el = await page.query_selector(s["item_seller_type"]) if s.get("item_seller_type") else None
            if seller_el:
                seller_text = (await seller_el.inner_text()).lower()
                if any(w in seller_text for w in ("частное лицо", "собственник", "частник")):
                    listing.is_private = True
                elif any(w in seller_text for w in ("компания", "автосалон", "дилер", "магазин")):
                    listing.is_private = False
            return listing
        finally:
            await page.close()

    async def _type_like_human(self, page, selector: str, text: str) -> None:
        box = page.locator(selector).first
        await box.click()
        await self._pause(0.5, 1.5)
        for chunk in re.findall(r".{1,12}", text, flags=re.S):
            await box.type(chunk, delay=random.randint(35, 110))
            if random.random() < 0.15:
                await self._pause(0.3, 1.2)

    async def send(self, account: str, listing: Listing, text: str) -> None:
        s = self.sel
        ctx = await self._context(account)
        page = await ctx.new_page()
        try:
            await page.goto(listing.url, wait_until="domcontentloaded", timeout=60000)
            await self._pause(2, 5)
            await self._check_blocked(page)
            if await page.query_selector(s["login_marker"]):
                raise PlatformError(f"{self.name}/{account}: вход в аккаунт слетел — повторите buyout.login")
            await page.locator(s["write_button"]).first.click()
            await self._pause(2, 4)
            await self._type_like_human(page, s["message_input"], text)
            await self._pause(0.7, 2)
            send_btn = page.locator(s["send_button"]).first
            if await send_btn.count():
                await send_btn.click()
            else:
                await page.keyboard.press("Enter")
            await self._pause(1.5, 3)
            await self._save_session(account)
        finally:
            await page.close()

    async def incoming(self, account: str) -> List[IncomingMessage]:
        s = self.sel
        ctx = await self._context(account)
        page = await ctx.new_page()
        out: List[IncomingMessage] = []
        try:
            await page.goto(s["inbox_url"], wait_until="domcontentloaded", timeout=60000)
            await self._pause(2, 4)
            await self._check_blocked(page)
            unread = await page.query_selector_all(s["inbox_unread"])
            for chat in unread[:20]:
                await chat.click()
                await self._pause(1.5, 3)
                link = await page.query_selector(s["chat_item_link"])
                href = (await link.get_attribute("href")) if link else ""
                m = re.search(r"(\d{6,})", href or "")
                if not m:
                    continue
                msgs = await page.query_selector_all(s["chat_incoming"])
                if not msgs:
                    continue
                # Берём последние входящие сообщения подряд (продавец мог написать несколько)
                texts = [(await el.inner_text()).strip() for el in msgs[-3:]]
                out.append(IncomingMessage(listing_key=f"{self.name}:{m.group(1)}",
                                           texts=[t for t in texts if t], chat_ref=page.url))
            await self._save_session(account)
            return out
        finally:
            await page.close()

    async def reset_account(self, account: str) -> None:
        ctx = self._contexts.pop(account, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        for ctx in self._contexts.values():
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._contexts.clear()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None
