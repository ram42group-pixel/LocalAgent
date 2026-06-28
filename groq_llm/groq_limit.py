# -*- coding: utf-8 -*-
#groq_limit.py — Groqの利用制限を確認する
"""
Groq の残量はレスポンスヘッダで返る（専用の残量APIは無い）。
  x-ratelimit-remaining-requests : 残りリクエスト数（1日）
  x-ratelimit-remaining-tokens   : 残りトークン数（1分）
よって「直近の send() が保存したヘッダ」を読むのが確認方法。
枠を超えると send() が LimitError(retry_after) を投げる。
日次リセットは UTC 0:00（JST 9:00）。
"""
from groq_llm import groq_control


def get_limits() -> dict | None:
    """直近の send() で観測した残量。まだ一度も送っていなければ None。"""
    return dict(groq_control.LAST_LIMITS) or None


def probe() -> dict:
    """1回ごく短いリクエストを送って最新の残量を取得する（トークンを少し消費する）。"""
    groq_control.send(text="hi")
    return dict(groq_control.LAST_LIMITS)


if __name__ == "__main__":
    print("保存済み:", get_limits())
    print("計測   :", probe())
