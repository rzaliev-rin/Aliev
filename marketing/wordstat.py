# -*- coding: utf-8 -*-
"""
Клиент Яндекс Вордстат API (https://api.wordstat.yandex.net, OAuth-токен).

Методы:
  POST /v1/topRequests  — фраза -> totalCount, топ запросов с этим словом
                          (topRequests) и похожие запросы (associations)
  POST /v1/dynamics     — помесячная динамика фразы (сезонность)

Ответы кэшируются на диск (MARKETING_CACHE_DIR), чтобы повторный запуск
не тратил квоту: срок кэша — 7 дней.
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

import requests

from marketing import settings

CACHE_TTL_SECONDS = 7 * 24 * 3600


class WordstatError(Exception):
    pass


@dataclass
class Phrase:
    phrase: str
    count: int
    seed: str = ""
    source: str = "top"  # top | assoc


@dataclass
class SeedResult:
    seed: str
    total: int
    phrases: List[Phrase] = field(default_factory=list)
    dynamics: List[dict] = field(default_factory=list)  # [{"date": "2026-09-01", "count": N}]


class WordstatClient:
    def __init__(self, token: str = settings.WORDSTAT_TOKEN, regions: Optional[List[int]] = None,
                 cache_dir: str = settings.MARKETING_CACHE_DIR, pause: float = settings.WORDSTAT_PAUSE):
        if not token:
            raise WordstatError("Не задан WORDSTAT_TOKEN (OAuth-токен Яндекс Вордстат API).")
        self.token = token
        self.regions = regions or settings.WORDSTAT_REGIONS
        self.cache_dir = cache_dir
        self.pause = pause
        os.makedirs(cache_dir, exist_ok=True)

    def _call(self, method: str, body: dict) -> dict:
        key = hashlib.sha1(json.dumps([method, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        path = os.path.join(self.cache_dir, f"{key}.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_TTL_SECONDS:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        url = f"{settings.WORDSTAT_API_BASE}/v1/{method}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json;charset=utf-8"}
        for attempt in range(4):
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code == 429:  # квота в секунду — подождать и повторить
                time.sleep(2 ** attempt)
                continue
            if resp.status_code != 200:
                raise WordstatError(f"Wordstat {method} вернул {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            time.sleep(self.pause)
            return data
        raise WordstatError(f"Wordstat {method}: превышена квота запросов (429)")

    def top_requests(self, seed: str, num_phrases: int = settings.WORDSTAT_NUM_PHRASES) -> SeedResult:
        data = self._call("topRequests", {
            "phrase": seed, "numPhrases": num_phrases, "regions": self.regions, "devices": ["all"],
        })
        result = SeedResult(seed=seed, total=int(data.get("totalCount") or 0))
        for source, key in (("top", "topRequests"), ("assoc", "associations")):
            for row in data.get(key) or []:
                result.phrases.append(Phrase(row["phrase"], int(row.get("count") or 0), seed, source))
        return result

    def dynamics(self, seed: str, from_date: str, to_date: str) -> List[dict]:
        data = self._call("dynamics", {
            "phrase": seed, "period": "monthly", "fromDate": from_date, "toDate": to_date,
            "regions": self.regions, "devices": ["all"],
        })
        return data.get("dynamics") or []
