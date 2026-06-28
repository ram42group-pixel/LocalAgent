# -*- coding: utf-8 -*-
#open_router_limit.py — OpenRouterの利用制限を確認する
"""
OpenRouter は専用エンドポイント GET /api/v1/key で残量を確認できる
（SDKに該当メソッドが無いので requests で直接叩く）。
無料枠: 20 RPM・50リクエスト/日（$10課金で1,000/日）。日次リセットは UTC 0:00 目安。
枠を超えると send() が LimitError(retry_after) を投げる。
"""
import requests

from get_env import env_controler as env
from open_router import open_router_control


def get_limits() -> dict | None:
    """キーの使用量・上限・残りを取得する（リクエストを1回消費しない: 管理APIのため枠外）。"""
    import api_keys
    key = api_keys.get_key("open_router")
    if not key:
        return None
    res = requests.get(f"{open_router_control.BASE_URL}/key",
                       headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if not res.ok:
        return {"error": res.status_code}
    d = res.json().get("data", {})
    return {
        "usage": d.get("usage"),
        "limit": d.get("limit"),
        "limit_remaining": d.get("limit_remaining"),
        "is_free_tier": d.get("is_free_tier"),
        "rate_limit": d.get("rate_limit"),
    }


if __name__ == "__main__":
    print("残量:", get_limits())
