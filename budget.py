# -*- coding: utf-8 -*-
#budget.py — Budget Meter プラグイン（トークン消費の見える化）
"""
registry の observer に登録し、send/recv ごとに概算トークンを積算する。
無料枠管理のため「このターンで何トークン使ったか」「プロバイダ別累計」を保持。
厳密なトークナイザは使わず len//2 で概算（判定用途には十分）。
"""
from providers.registry import add_observer

# provider別の累計と直近呼び出しの記録
TOTALS: dict[str, dict] = {}
LAST: dict = {}


def approx_tokens(text: str) -> int:
    return max(1, len(text or "") // 2)


def _on_event(event: str, payload: dict):
    if event == "send":
        LAST["in"] = approx_tokens(payload.get("prompt_preview", ""))
    elif event == "recv":
        p = payload["provider"]
        tin = LAST.get("in", 0)
        tout = approx_tokens(payload.get("content", ""))
        rec = TOTALS.setdefault(p, {"calls": 0, "tokens_in": 0, "tokens_out": 0})
        rec["calls"] += 1
        rec["tokens_in"] += tin
        rec["tokens_out"] += tout
        LAST.update({"provider": p, "model": payload.get("model"),
                     "in": tin, "out": tout, "ms": payload.get("ms")})


def snapshot() -> dict:
    return {"totals": TOTALS, "last": dict(LAST)}


def register():
    add_observer(_on_event)
