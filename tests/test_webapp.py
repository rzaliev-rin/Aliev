# -*- coding: utf-8 -*-
"""Проверка подписи мини-приложения MAX. Запуск: python tests/test_webapp.py"""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import webapp  # noqa: E402

TOKEN = "test-token"


def _signed(params: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**params, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def _params(**kw):
    p = {"auth_date": str(int(time.time())), "query_id": "q",
         "user": json.dumps({"id": 42, "first_name": "Иван"}, ensure_ascii=False)}
    p.update(kw)
    return p


def test_valid():
    assert webapp.validate_init_data(_signed(_params()), TOKEN)["id"] == 42


def test_wrong_token_or_tampered():
    assert webapp.validate_init_data(_signed(_params()), "other") is None
    data = _signed(_params()).replace("%22id%22%3A+42", "%22id%22%3A+1")
    assert webapp.validate_init_data(data, TOKEN) is None


def test_expired():
    assert webapp.validate_init_data(_signed(_params(auth_date=str(int(time.time()) - 3 * 86400))), TOKEN) is None


def test_duplicate_param_rejected():
    assert webapp.validate_init_data(_signed(_params()) + "&query_id=x", TOKEN) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
