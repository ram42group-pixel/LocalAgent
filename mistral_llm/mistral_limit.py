# -*- coding: utf-8 -*-
#mistral_limit.py — Mistralの利用制限を確認する
"""
Mistralはレスポンスヘッダでレート情報を返す場合がある。
専用の残量APIは無いため、直近の send() が保存した情報を読む。
枠を超えると send() が LimitError を投げる。
"""
from mistral_llm import mistral_control


def get_limits() -> dict | None:
    return dict(mistral_control.LAST_LIMITS) or None


def probe() -> dict:
    mistral_control.send(text="hi")
    return dict(mistral_control.LAST_LIMITS)


if __name__ == "__main__":
    print("limits:", get_limits())
