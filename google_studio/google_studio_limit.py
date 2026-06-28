# -*- coding: utf-8 -*-
#google_studio_limit.py — Google AI Studio (Gemini) の利用制限を確認する
"""
Gemini には「残量」を返すAPIやヘッダが無い。確認手段は次の2つだけ:
  1. 枠を超えたら send() が LimitError(429 / RESOURCE_EXHAUSTED) を投げる
  2. 自前で消費を記録する（send() が usage_metadata を USAGE に積算している）
日次リセット(RPD)は太平洋時間の深夜（JST 17時頃）。上限はキー単位ではなくプロジェクト単位。
503(UNAVAILABLE) は枠超過ではなくサーバ混雑（send側でリトライ済み）。
"""
from google_studio import google_studio_control


def get_usage() -> dict:
    """このプロセスでの累計消費（calls / total_tokens / 直近のusage_metadata）。"""
    return dict(google_studio_control.USAGE)


if __name__ == "__main__":
    print("消費記録:", get_usage())
    print("※ Geminiは残量APIが無いため、超過は send() の LimitError で検知する")
