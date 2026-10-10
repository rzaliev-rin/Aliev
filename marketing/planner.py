# -*- coding: utf-8 -*-
"""
Маркетинговый план из кластеров Вордстата и склада:
  - лендинги (коммерческий спрос по модели / кредит / trade-in / ПИ);
  - SEO-статьи и статьи блога (информационный спрос);
  - хуки (офферы) для объявлений и креативов — от интента и фактов склада;
  - структура кампаний Яндекс Директа + минус-слова;
  - контент-календарь на 12 недель.
Частоты — показы в месяц по региону из настроек (по умолчанию Екатеринбург).
"""
import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from marketing.semantics import COMMERCIAL, INFORMATIONAL, INTENT_TITLES, Cluster
from marketing.stock import ModelGroup

# Минимальная частота кластера, чтобы под него делать отдельную страницу
LANDING_MIN_VOLUME = 150
ARTICLE_MIN_VOLUME = 50


@dataclass
class Landing:
    title: str
    url: str
    intent: str
    volume: int
    keys: List[str]
    blocks: List[str]
    priority: int = 0


@dataclass
class Article:
    title: str
    kind: str          # SEO | Блог
    model: str
    intent: str
    volume: int
    keys: List[str]
    outline: List[str]
    cta: str
    week: int = 0


@dataclass
class Hook:
    model: str
    intent: str
    headline: str      # заголовок объявления (≤56 симв.)
    text: str          # текст объявления (≤81 симв.)
    creative: str      # идея для баннера/видео/поста
    volume: int


@dataclass
class Campaign:
    name: str
    type: str
    groups: List[Dict]
    note: str = ""


@dataclass
class Plan:
    dealer: str
    city: str
    generated: str
    models: List[ModelGroup]
    clusters: List[Cluster]
    landings: List[Landing] = field(default_factory=list)
    articles: List[Article] = field(default_factory=list)
    hooks: List[Hook] = field(default_factory=list)
    campaigns: List[Campaign] = field(default_factory=list)
    minus_words: List[str] = field(default_factory=list)
    seasonality: Dict[int, float] = field(default_factory=dict)


def slug(text: str) -> str:
    table = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "abvgdeejziiklmnoprstufhccss_y_eua")
    out = "".join(c if c.isalnum() else "-" for c in text.lower().translate(table))
    return "-".join(p for p in out.split("-") if p)


def price_text(g: Optional[ModelGroup]) -> str:
    if not g or not g.price_min:
        return ""
    return f"от {g.price_min / 1e6:.2f} млн ₽".replace(".", ",")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _volume(clusters: List[Cluster], model: str, intents) -> int:
    return sum(c.volume for c in clusters if c.model == model and c.intent in intents)


def _keys(clusters: List[Cluster], model: str, intents, n: int = 15) -> List[str]:
    phrases = [p for c in clusters if c.model == model and c.intent in intents for p in c.phrases]
    return [f"{p.phrase} — {p.count}" for p in sorted(phrases, key=lambda p: -p.count)[:n]]


def build_landings(clusters: List[Cluster], models: List[ModelGroup], city: str) -> List[Landing]:
    landings: List[Landing] = []
    for g in models:
        vol = _volume(clusters, g.model, {"buy", "trims", "general"})
        if vol >= LANDING_MIN_VOLUME or g.count:
            landings.append(Landing(
                title=f"Toyota {g.model} купить в {city}е — {g.count} шт. в наличии",
                url=f"/toyota/{slug(g.model)}/", intent="buy", volume=vol,
                keys=_keys(clusters, g.model, {"buy", "trims", "general"}),
                blocks=["Первый экран: модель + цена " + (price_text(g) or "«от …»") + " + «в наличии в Екатеринбурге»",
                        "Живой склад из MaxPoster (фильтр по комплектации/цвету), реальные фото",
                        "Комплектации и отличия (таблица), что входит в цену",
                        "Гарантия и сервис для авто параллельного импорта",
                        "Кредит: расчёт платежа; Trade-in: оценка за 30 минут",
                        "Форма: «Забронировать», «Тест-драйв», «Узнать финальную цену»"],
            ))
        credit_vol = _volume(clusters, g.model, {"credit"})
        if credit_vol >= LANDING_MIN_VOLUME:
            landings.append(Landing(
                title=f"Toyota {g.model} в кредит в {city}е", url=f"/toyota/{slug(g.model)}/kredit/",
                intent="credit", volume=credit_vol, keys=_keys(clusters, g.model, {"credit"}),
                blocks=["Калькулятор платежа (первый взнос / срок)", "Банки-партнёры, одобрение за 1 день",
                        "Кредит + Trade-in в зачёт взноса", "Форма заявки на кредит"],
            ))
    brand_blocks = {
        "credit": ("Новая Toyota в кредит в Екатеринбурге", "/toyota/kredit/",
                   ["Калькулятор по всем моделям склада", "Условия банков", "Заявка онлайн"]),
        "tradein": ("Trade-in на новую Toyota в Екатеринбурге", "/trade-in/",
                    ["Онлайн-оценка авто", "Выгода при обмене", "Сделка за 1 день", "Форма оценки"]),
        "pi": ("Toyota параллельного импорта в наличии в Екатеринбурге", "/toyota/parallelnyi-import/",
               ["Откуда авто (Китай/ОАЭ/Япония) и чем отличаются", "Гарантия дилера, ТО, ЭПТС",
                "Почему дешевле ожидания «под заказ»: машины уже в Екатеринбурге", "Склад всех моделей"]),
        "buy": (f"Автосалон Toyota в {city}е — новые авто в наличии", "/toyota/",
                ["Склад всех моделей с ценами", "Акции месяца", "Кредит / Trade-in", "Контакты, карта, режим"]),
    }
    for intent, (title, url, blocks) in brand_blocks.items():
        vol = sum(c.volume for c in clusters if c.intent == intent and c.model in ("Бренд Toyota", "Без бренда"))
        vol_all = sum(c.volume for c in clusters if c.intent == intent)
        if vol_all >= LANDING_MIN_VOLUME or intent == "buy":
            landings.append(Landing(title, url, intent, vol,
                                    _keys(clusters, "Бренд Toyota", {intent}) + _keys(clusters, "Без бренда", {intent}, 5),
                                    blocks))
    for i, l in enumerate(sorted(landings, key=lambda l: -l.volume), 1):
        l.priority = i
    return sorted(landings, key=lambda l: l.priority)


ARTICLE_TEMPLATES = {
    "reviews": ("Toyota {m}: отзывы владельцев, плюсы и минусы", "SEO",
                ["Кому подходит {m}", "Плюсы по отзывам", "Минусы и как с ними живут", "Расходы на владение",
                 "Вывод + какие версии есть в наличии в Екатеринбурге"]),
    "specs": ("Toyota {m} {y}: характеристики, размеры, клиренс и расход", "SEO",
              ["Таблица характеристик по версиям", "Габариты и багажник", "Двигатели, расход в городе/трассе",
               "Клиренс и проходимость зимой на Урале", "Какая версия на складе"]),
    "trims": ("Комплектации и цены Toyota {m} {y} в Екатеринбурге", "SEO",
              ["Все комплектации таблицей", "Чем отличаются (что реально стоит доплаты)", "Цвета",
               "Цены на складе сейчас", "Как забронировать"]),
    "compare": ("Toyota {m} или конкуренты: что выбрать в {y}", "SEO",
                ["С кем сравнивают {m} (по запросам)", "Цена / оснащение / расход", "Ликвидность при продаже",
                 "Итог: кому что", "Тест-драйв в салоне"]),
    "pi": ("Toyota {m} параллельного импорта: гарантия, ТО, документы", "SEO",
           ["Откуда машина и чем отличается от «официальной»", "Гарантия и где обслуживать в Екатеринбурге",
            "ЭПТС, утильсбор, растаможка — что уже входит в цену", "Риски и как мы их снимаем", "Склад"]),
    "general": ("Toyota {m} {y}: всё о модели и цены в Екатеринбурге", "Блог",
                ["Что нового в {y}", "Версии и цены", "Кому подходит", "Фото с нашего склада"]),
}
BRAND_ARTICLES = [
    ("pi", "Параллельный импорт Toyota в 2026: как купить без риска", "SEO",
     ["Что такое ПИ и почему цены отличаются", "Гарантия, сервис, запчасти", "ЭПТС и регистрация",
      "Чек-лист проверки машины", "Почему выгоднее купить в наличии, а не «под заказ»"]),
    ("pi", "Toyota из Китая, ОАЭ или Японии: в чём разница", "SEO",
     ["Рынки и версии (Frontlander/Wildlander vs Corolla Cross/RAV4)", "Оснащение и адаптация к зиме",
      "Мультимедиа и русификация", "Что выбрать"]),
    ("credit", "Как купить новую Toyota в кредит: платёж, взнос, одобрение", "Блог",
     ["Условия банков сейчас", "Пример расчёта по моделям склада", "Кредит + trade-in", "Документы"]),
    ("tradein", "Trade-in в Екатеринбурге: как получить максимум за старое авто", "Блог",
     ["Как оценивают", "Что повышает цену", "Пример: обмен на Toyota", "Онлайн-оценка"]),
]


def build_articles(clusters: List[Cluster], models: List[ModelGroup], year: int) -> List[Article]:
    articles: List[Article] = []
    model_names = {g.model for g in models}
    for c in clusters:
        if c.intent not in INFORMATIONAL or c.volume < ARTICLE_MIN_VOLUME or c.model not in model_names:
            continue
        title, kind, outline = ARTICLE_TEMPLATES[c.intent]
        articles.append(Article(
            title=title.format(m=c.model, y=year), kind=kind, model=c.model, intent=c.intent, volume=c.volume,
            keys=[f"{p.phrase} — {p.count}" for p in c.top[:12]],
            outline=[o.format(m=c.model, y=year) for o in outline],
            cta=f"Toyota {c.model} в наличии в Екатеринбурге → /toyota/{slug(c.model)}/",
        ))
    for intent, title, kind, outline in BRAND_ARTICLES:
        vol = sum(c.volume for c in clusters if c.intent == intent)
        keys = [f"{p.phrase} — {p.count}" for c in clusters if c.intent == intent for p in c.top[:3]][:12]
        articles.append(Article(title, kind, "Бренд Toyota", intent, vol, keys, outline,
                                "Склад Toyota в Екатеринбурге → /toyota/"))
    articles.sort(key=lambda a: -a.volume)
    for i, a in enumerate(articles):  # 2 материала в неделю, самые частотные — первыми
        a.week = i // 2 + 1
    return articles


def build_hooks(clusters: List[Cluster], models: List[ModelGroup], city: str) -> List[Hook]:
    hooks: List[Hook] = []
    for g in models:
        price = price_text(g)
        vol = {i: _volume(clusters, g.model, {i}) for i in INTENT_TITLES}
        stock = f"{g.count} шт. в наличии" if g.count else "В наличии"
        variants = [
            ("buy", f"Toyota {g.model} в {city}е {price}", f"{stock}. Без ожидания и предоплаты за «под заказ». Бронь онлайн!",
             f"Фото реальных {g.model} на нашей площадке + цена крупно + «забрать сегодня»"),
            ("credit", f"Toyota {g.model} в кредит в {city}е", "Платёж рассчитаем за 5 минут, одобрение за 1 день. Trade-in в зачёт.",
             "Платёж в месяц крупно вместо полной цены; калькулятор в сторис"),
            ("tradein", f"Обменяйте авто на Toyota {g.model}", "Оценка за 30 минут, выгода при trade-in. Сделка за один день.",
             "До/после: старое авто → новая Toyota; цифра выгоды"),
            ("pi", f"Toyota {g.model} с гарантией дилера", "Параллельный импорт без рисков: проверка, ЭПТС, сервис в Екатеринбурге.",
             "«Что проверяем перед продажей» — чек-лист / видео с осмотра"),
            ("compare", f"Toyota {g.model} или конкуренты?", "Сравните на тест-драйве. Честная таблица цен и оснащения.",
             "Сравнение бок о бок; опрос в соцсетях «что выбрали бы вы»"),
            ("reviews", f"Toyota {g.model}: правда владельцев", "Плюсы и минусы без прикрас + машины в наличии.",
             "Видео-обзор менеджера на складе, 30–60 сек"),
        ]
        for intent, head, text, creative in variants:
            if intent == "buy" or vol.get(intent, 0) >= ARTICLE_MIN_VOLUME:
                hooks.append(Hook(g.model, intent, _cut(head, 56), _cut(text, 81), creative, vol.get(intent, 0)))
    return sorted(hooks, key=lambda h: -h.volume)


def build_campaigns(clusters: List[Cluster], models: List[ModelGroup]) -> List[Campaign]:
    def group(model, intents, n=30):
        return {"group": f"{model} — " + "/".join(INTENT_TITLES[i].split(" /")[0] for i in intents),
                "volume": _volume(clusters, model, intents),
                "keys": [k.split(" — ")[0] for k in _keys(clusters, model, intents, n)]}
    names = [g.model for g in models]
    return [
        Campaign("Поиск | Модели | Покупка", "Поиск (ЕПК)", [group(m, {"buy", "trims"}) for m in names],
                 "Ставка по модели — пропорционально остатку склада и марже"),
        Campaign("Поиск | Кредит", "Поиск (ЕПК)", [group(m, {"credit"}) for m in names + ["Бренд Toyota"]]),
        Campaign("Поиск | Trade-in", "Поиск (ЕПК)", [group(m, {"tradein"}) for m in names + ["Бренд Toyota", "Без бренда"]]),
        Campaign("Поиск | Бренд и ПИ", "Поиск (ЕПК)", [group("Бренд Toyota", {"buy", "pi", "general"}),
                                                       group("Без бренда", {"buy", "pi"})]),
        Campaign("РСЯ | Интерес к моделям", "РСЯ / ЕПК", [group(m, {"reviews", "specs", "compare", "general"}) for m in names],
                 "Широкий охват по информационным фразам, креативы из «Хуков»"),
        Campaign("Ретаргетинг | Склад", "Товарная кампания по фиду MaxPoster", [],
                 "Фид склада → динамические объявления с ценой и фото; аудитории посетителей лендингов"),
    ]


def build_plan(dealer: str, city: str, models: List[ModelGroup], clusters: List[Cluster],
               minus: List[str], season: Dict[int, float], today: Optional[datetime.date] = None) -> Plan:
    today = today or datetime.date.today()
    plan = Plan(dealer, city, today.isoformat(), models, clusters, minus_words=minus, seasonality=season)
    plan.landings = build_landings(clusters, models, city)
    plan.articles = build_articles(clusters, models, today.year)
    plan.hooks = build_hooks(clusters, models, city)
    plan.campaigns = build_campaigns(clusters, models)
    return plan


def demand_summary(plan: Plan) -> List[Dict]:
    """Спрос по модели: коммерческий / информационный / всего + остаток склада."""
    rows = []
    for g in plan.models:
        com = _volume(plan.clusters, g.model, COMMERCIAL)
        info = _volume(plan.clusters, g.model, INFORMATIONAL)
        rows.append({"model": g.model, "stock": g.count, "price_from": g.price_min, "commercial": com,
                     "informational": info, "total": com + info,
                     "demand_per_car": round((com + info) / g.count) if g.count else None})
    return sorted(rows, key=lambda r: -r["total"])
