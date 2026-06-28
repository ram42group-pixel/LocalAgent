# -*- coding: utf-8 -*-
#cerebras_limit.py — Cerebrasの利用制限を確認する
"""
Cerebras の残量はレスポンスヘッダで返る（Groqとヘッダ名が違う）。
  x-ratelimit-remaining-requests-day  : 残りリクエスト数（1日）
  x-ratelimit-remaining-tokens-minute : 残りトークン数（1分）
無料枠は 100万トークン/日・30RPM・約8Kコンテキスト。
日次リセットは UTC 0:00（JST 9:00）。
枠を超えると send() が LimitError(retry_after) を投げる。
"""
from cerebras_llm import cerebras_control


def get_limits() -> dict | None:
    """直近の send() で観測した残量。まだ一度も送っていなければ None。"""
    return dict(cerebras_control.LAST_LIMITS) or None


def probe() -> dict:
    """1回ごく短いリクエストを送って最新の残量を取得する（トークンを少し消費する）。"""
    cerebras_control.send(text="hi")
    return dict(cerebras_control.LAST_LIMITS)


if __name__ == "__main__":
    print("保存済み:", get_limits())
    print("計測   :", probe())
