# -*- coding: utf-8 -*-
#session.py — 直近の実行状態を保存し「前回の続き」を引き継げるようにする
"""
run_agent の最後に goal/objectives/進捗を session.json へ保存。
「前回の続き」「続行」等の要望が来たら、保存済みの状態から再開する。
"""
from __future__ import annotations

import json
import os
import re
import time

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")

# 続行を示す言い回し
_CONT = re.compile(r"(前回|さっき|続き|続行|つづき|再開|途中から|resume|continue)", re.I)


def is_continue(request: str) -> bool:
    """「前回の続き」系の要望か判定する。
    新しい具体的な命令（長文）に続行語がたまたま含まれるだけのケースは除外し、
    短く明示的に続行を求めている場合のみ True にする（新命令の乗っ取りを防ぐ）。"""
    req = (request or "").strip()
    if not _CONT.search(req):
        return False
    # 長い命令文は新規タスクとみなす（続行語を含んでも）
    if len(req) > 25:
        return False
    return True


def save(goal: str, objectives: list[str], index: int, tags: list[str],
         results: list) -> None:
    data = {
        "goal": goal, "objectives": objectives, "index": index, "tags": tags,
        "results": results, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load() -> dict | None:
    if not os.path.exists(_FILE):
        return None
    try:
        with open(_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def clear() -> None:
    if os.path.exists(_FILE):
        os.remove(_FILE)
