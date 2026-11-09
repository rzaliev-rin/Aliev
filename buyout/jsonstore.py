# -*- coding: utf-8 -*-
"""Маленькое JSON-хранилище с атомарной записью (как в dealer_chats.py)."""
import copy
import json
import os
import threading

_lock = threading.RLock()


def load(path: str, default):
    with _lock:
        if not os.path.exists(path):
            return copy.deepcopy(default)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001 — битый файл: не теряем работу бота
            return copy.deepcopy(default)


def save(path: str, data) -> None:
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
