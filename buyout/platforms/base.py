# -*- coding: utf-8 -*-
"""Общий интерфейс площадки: найти объявления, написать продавцу, забрать новые ответы."""
from dataclasses import dataclass
from typing import List

from buyout.listings import Listing


@dataclass
class IncomingMessage:
    listing_key: str     # "avito:1234567890" — по нему находим диалог
    texts: List[str]     # последние входящие сообщения продавца (старые отсеет движок)
    chat_ref: str = ""   # ID/ссылка чата на площадке (если отличается от объявления)


class PlatformError(Exception):
    """Ошибка площадки: не залогинен аккаунт, капча, изменилась вёрстка и т.п."""


class Platform:
    name = ""

    async def search(self, search_url: str, limit: int = 50) -> List[Listing]:
        """Объявления по ссылке поиска (фильтры, настроенные прямо на сайте)."""
        raise NotImplementedError

    async def details(self, listing: Listing) -> Listing:
        """Дозаполнить объявление со страницы карточки (описание, владельцы, продавец)."""
        return listing

    async def send(self, account: str, listing: Listing, text: str) -> None:
        """Написать продавцу в чат площадки по объявлению (первое сообщение или ответ)."""
        raise NotImplementedError

    async def incoming(self, account: str) -> List[IncomingMessage]:
        """Новые (непрочитанные) сообщения продавцов на этом аккаунте."""
        raise NotImplementedError

    async def reset_account(self, account: str) -> None:
        """Забыть открытую сессию аккаунта (после того как прислали новый файл входа)."""

    async def close(self) -> None:
        pass
