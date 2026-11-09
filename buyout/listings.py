# -*- coding: utf-8 -*-
"""Объявление с площадки и проверка его по фильтрам профиля."""
import re
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Listing:
    platform: str               # avito / autoru / drom
    listing_id: str             # ID объявления на площадке
    url: str
    title: str = ""             # «Toyota Camry 2.5 AT, 2019»
    brand: str = ""
    model: str = ""
    year: Optional[int] = None
    price: Optional[int] = None
    mileage: Optional[int] = None
    owners: Optional[int] = None
    transmission: str = ""
    city: str = ""
    description: str = ""
    seller_name: str = ""
    seller_id: str = ""
    is_private: Optional[bool] = None   # None — не удалось определить
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.listing_id}"

    def fingerprint(self) -> str:
        """Одна и та же машина часто висит сразу на трёх площадках — не пишем продавцу трижды."""
        if not (self.brand and self.year and self.mileage):
            return ""
        return "|".join([_norm(self.brand), _norm(self.model), str(self.year), str(self.mileage // 1000)])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)

    def summary(self) -> str:
        parts = [self.title or f"{self.brand} {self.model}".strip()]
        if self.year:
            parts.append(f"{self.year} г.")
        if self.mileage is not None:
            parts.append(f"{self.mileage:,} км".replace(",", " "))
        if self.price:
            parts.append(f"{self.price:,} ₽".replace(",", " "))
        if self.city:
            parts.append(self.city)
        return ", ".join(p for p in parts if p)


def _norm(s: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", (s or "").lower().replace("ё", "е"))


def _in_list(value: str, allowed: list) -> bool:
    if not allowed:
        return True
    v = _norm(value)
    return bool(v) and any(_norm(a) == v or _norm(a) in v for a in allowed)


def rejection_reason(listing: Listing, filters: dict) -> Optional[str]:
    """None — объявление подходит; иначе — почему отсеяли (для журнала)."""
    f = filters
    if f.get("brands") and not _in_list(listing.brand or listing.title, f["brands"]):
        return "марка"
    if f.get("models") and not _in_list(listing.model or listing.title, f["models"]):
        return "модель"
    if listing.year is not None:
        if f.get("year_from") and listing.year < f["year_from"]:
            return "год"
        if f.get("year_to") and listing.year > f["year_to"]:
            return "год"
    if listing.price is not None:
        if f.get("price_from") and listing.price < f["price_from"]:
            return "цена"
        if f.get("price_to") and listing.price > f["price_to"]:
            return "цена"
    if f.get("mileage_max") and listing.mileage is not None and listing.mileage > f["mileage_max"]:
        return "пробег"
    if f.get("owners_max") and listing.owners is not None and listing.owners > f["owners_max"]:
        return "владельцы"
    if f.get("transmission") and listing.transmission and not _in_list(listing.transmission, f["transmission"]):
        return "КПП"
    if f.get("only_private") and listing.is_private is False:
        return "не частник"
    text = f"{listing.title} {listing.description}".lower().replace("ё", "е")
    for word in f.get("stop_words") or []:
        w = word.lower().replace("ё", "е").strip()
        if w and w in text:
            return f"стоп-слово «{word}»"
    return None
