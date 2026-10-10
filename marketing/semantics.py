# -*- coding: utf-8 -*-
"""
Семантика: сиды из склада -> фразы Вордстата -> интент -> кластеры
(модель × интент). Всё на правилах (маркерах), без внешних библиотек.
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from marketing.stock import ModelGroup
from marketing.wordstat import Phrase

# Модель -> как её пишут по-русски (и частые ошибки)
MODEL_ALIASES: Dict[str, List[str]] = {
    "camry": ["камри", "камри"],
    "rav4": ["рав 4", "рав4", "рав-4"],
    "land cruiser prado": ["прадо", "ленд крузер прадо", "лэнд крузер прадо"],
    "land cruiser": ["ленд крузер", "лэнд крузер", "крузак", "lc 300", "лк 300"],
    "highlander": ["хайлендер", "хайлэндер"],
    "corolla": ["королла", "корола"],
    "corolla cross": ["королла кросс"],
    "fortuner": ["фортунер"],
    "hilux": ["хайлюкс", "хайлакс"],
    "frontlander": ["фронтлендер", "фронтлэндер"],
    "wildlander": ["вайлдлендер", "вилдлендер"],
    "bz3": ["бз3"],
    "bz4x": ["бз4х", "bz 4x"],
    "alphard": ["альфард"],
    "sienna": ["сиенна"],
    "granvia": ["гранвия"],
    "crown": ["краун"],
    "c-hr": ["c hr", "сх р", "chr"],
    "levin": ["левин"],
    "sequoia": ["секвойя"],
    "tundra": ["тундра"],
    "venza": ["венза"],
    "allion": ["аллион"],
}

BRAND_FORMS = ["toyota", "тойота", "тоёта", "таета"]

# Интенты: порядок важен (первый подошедший)
INTENT_MARKERS: List[Tuple[str, List[str]]] = [
    ("minus", [
        "б/у", " бу", "бу ", "с пробегом", "пробег", "аренд", "прокат", "шин", "диск", "коврик", "чехл",
        "запчаст", "разбор", "ремонт", "ошибк", "предохранит", "магнитол", "игрушк", "масштаб", "модель 1:",
        "скачать", "гта", "gta", "обои", "раскраск", "аккумулятор", "масло", "фильтр", "лампочк", "фар",
        "бампер", "стекло", "тюнинг", "дром", "drom", "япония аукцион", "аукцион", "вакансии", "работа",
    ]),
    ("credit", ["кредит", "рассрочк", "лизинг", "ежемесячн", "платеж", "без первоначальн", "автокредит"]),
    ("tradein", ["трейд", "trade", "обмен", "выкуп", "сдать"]),
    ("pi", ["параллельн", "импорт", "из китая", "из оаэ", "из японии", "из кореи", "из дубая", "китайск",
            "китай", "оаэ", "растаможк", "утильсбор", "под заказ", "пригнать", "привезти", "гарантия"]),
    ("compare", [" или ", " vs ", "против", "сравнен", "что лучше", "лучше чем", "конкурент", "аналог"]),
    ("specs", ["характеристик", "размер", "клиренс", "габарит", "расход", "двигател", "мощност",
               "разгон", "багажник", "объем", "привод", "коробк", "вариатор", "гибрид", "топлив"]),
    ("trims", ["комплектац", "цвет", "салон фото", "фото", "интерьер", "версия", "модификац"]),
    ("reviews", ["отзыв", "обзор", "тест драйв", "тест-драйв", "минусы", "плюсы", "проблем", "надежн",
                 "болячк", "владел"]),
    ("buy", ["купить", "цена", "цены", "стоимост", "сколько стоит", "наличи", "продаж", "дилер", "салон",
             "автосалон", "официальн", "новый", "новая", "новые", "2025", "2026", "заказать", "акци", "скидк",
             "екатеринбург", "екб", "авито", "avito"]),
]

INTENT_TITLES = {
    "buy": "Покупка / цена / наличие",
    "credit": "Кредит / рассрочка / лизинг",
    "tradein": "Trade-in / обмен",
    "pi": "Параллельный импорт / гарантия / страна",
    "compare": "Сравнение",
    "specs": "Характеристики",
    "trims": "Комплектации / цвета",
    "reviews": "Отзывы / обзоры / тест-драйв",
    "general": "Общий интерес к модели",
    "minus": "Нецелевые (минус-слова)",
}
COMMERCIAL = {"buy", "credit", "tradein"}
INFORMATIONAL = {"pi", "compare", "specs", "trims", "reviews", "general"}


def aliases_for(model: str) -> List[str]:
    """Все написания модели, от длинных к коротким (Prado раньше Land Cruiser)."""
    key = model.lower().strip()
    forms = {key, key.replace(" ", ""), key.replace("-", " ")}
    # только самый длинный ключ: «Land Cruiser Prado 250» не должен ловить «ленд крузер 300»
    matched = [k for k in MODEL_ALIASES if key == k or key.startswith(k + " ")]
    if matched:
        alias_key = max(matched, key=len)
        forms.update(MODEL_ALIASES[alias_key])
        forms.add(alias_key)
    return sorted({f for f in forms if f}, key=len, reverse=True)


def build_seeds(models: Iterable[ModelGroup], city: str = "екатеринбург") -> List[str]:
    """Сиды для Вордстата: по каждой модели склада + общие по бренду и ПИ."""
    seeds = ["тойота", "toyota", "тойота купить", "тойота параллельный импорт", "тойота из китая",
             "тойота в кредит", "тойота трейд ин", f"автосалон тойота {city}", "новая тойота"]
    for g in models:
        name = g.model.lower()
        ru = next((a for a in aliases_for(g.model) if re.search("[а-я]", a)), None)
        seeds += [f"toyota {name}", f"тойота {ru}" if ru else f"тойота {name}", f"{ru or name} купить",
                  f"{ru or name} цена", f"{ru or name} отзывы"]
    seen, result = set(), []
    for s in seeds:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def classify(phrase: str) -> str:
    text = f" {phrase.lower()} "
    for intent, markers in INTENT_MARKERS:
        if any(m in text for m in markers):
            return intent
    return "general"


def detect_model(phrase: str, models: Iterable[str]) -> Optional[str]:
    text = phrase.lower()
    best, best_len = None, 0
    for model in models:
        for alias in aliases_for(model):
            if re.search(rf"(^|[^a-zа-я0-9]){re.escape(alias)}($|[^a-zа-я0-9])", text) and len(alias) > best_len:
                best, best_len = model, len(alias)
    return best


@dataclass
class Cluster:
    model: str          # модель склада или "Бренд"
    intent: str
    volume: int = 0     # сумма частот фраз (показов в месяц по региону)
    phrases: List[Phrase] = field(default_factory=list)

    @property
    def top(self) -> List[Phrase]:
        return sorted(self.phrases, key=lambda p: -p.count)


def build_clusters(phrases: Iterable[Phrase], models: List[str]) -> Tuple[List[Cluster], List[Phrase]]:
    """Возвращает (кластеры без минусов, нецелевые фразы для минус-слов)."""
    seen: Dict[str, Phrase] = {}
    for p in phrases:  # одна фраза может прийти из нескольких сидов — берём максимум
        key = " ".join(p.phrase.lower().split())
        if key not in seen or p.count > seen[key].count:
            seen[key] = Phrase(key, p.count, p.seed, p.source)
    clusters: Dict[Tuple[str, str], Cluster] = {}
    minus: List[Phrase] = []
    for p in seen.values():
        intent = classify(p.phrase)
        if intent == "minus":
            minus.append(p)
            continue
        model = detect_model(p.phrase, models)
        if model is None and not any(b in p.phrase for b in BRAND_FORMS):
            if intent == "general":
                continue  # фраза без бренда/модели и без понятного интента — шум ассоциаций
            model = "Без бренда"
        model = model or "Бренд Toyota"
        c = clusters.setdefault((model, intent), Cluster(model, intent))
        c.phrases.append(p)
        c.volume += p.count
    return sorted(clusters.values(), key=lambda c: -c.volume), sorted(minus, key=lambda p: -p.count)


MINUS_STEMS = [m.strip() for m in INTENT_MARKERS[0][1] if len(m.strip()) >= 2 and " " not in m.strip()]
MINUS_STOP = set(BRAND_FORMS) | {"в", "на", "с", "для", "и", "по", "от", "из", "купить", "цена", "новый"}


def minus_words(minus_phrases: Iterable[Phrase], limit: int = 80) -> List[str]:
    """Отдельные слова-маркеры из нецелевых фраз для списка минус-слов Директа."""
    weight: Dict[str, int] = defaultdict(int)
    for p in minus_phrases:
        for word in re.findall(r"[a-zа-яё/]+", p.phrase.lower()):
            if word in MINUS_STOP or len(word) < 3:
                continue
            if any(m in word for m in MINUS_STEMS):
                weight[word] += p.count
    return [w for w, _ in sorted(weight.items(), key=lambda kv: -kv[1])[:limit]]


def seasonality(dynamics: List[dict]) -> Dict[int, float]:
    """Индекс месяца (1..12): средняя частота месяца / средняя за год (1.0 = норма)."""
    by_month: Dict[int, List[int]] = defaultdict(list)
    for row in dynamics:
        month = int(str(row.get("date", ""))[5:7] or 0)
        if month:
            by_month[month].append(int(row.get("count") or 0))
    if not by_month:
        return {}
    means = {m: sum(v) / len(v) for m, v in by_month.items()}
    avg = sum(means.values()) / len(means) or 1
    return {m: round(v / avg, 2) for m, v in sorted(means.items())}
