# -*- coding: utf-8 -*-
#dedup.py — 同じ行動の二度実行を防ぎ、LLMへ「なぜダメか」を優しく説明する
"""
LLMが直前と同じコマンド等を繰り返すのを検知する。
止めるのではなく、5歳児に説くように噛み砕いた理由を返し、別の手を促す。
"""
from __future__ import annotations

import json


def fingerprint(action: dict) -> str:
    """行動の同一性キー。type＋効果を持つ主要フィールドだけで作る（reasonは無視）。"""
    t = action.get("type", "")
    key = {
        "command": action.get("command"),
        "file": (action.get("action"), action.get("path"), action.get("content")),
        "code": (action.get("language"), action.get("code")),
        "web_search": action.get("query"),
        "assist": action.get("message"),
    }.get(t, json.dumps(action, sort_keys=True, ensure_ascii=False))
    return f"{t}:{key}"


# 5歳児に説くトーン（比喩）。LLMへ返すフィードバック文を作る
def explain_duplicate(action: dict) -> str:
    t = action.get("type", "その操作")
    what = (action.get("command") or action.get("path")
            or action.get("query") or action.get("message") or t)
    return (
        f"ちょっと待ってね。それ（{what}）は、さっきもう一回やったことと"
        "まったく同じなんだ。\n"
        "同じ積み木をもう一度同じ場所に置いても、塔は高くならないよね？\n"
        "もう答えは前の結果に出ているから、それを見て『次の新しい一歩』を考えよう。\n"
        "・前の結果から分かったことを使う\n"
        "・別のやり方や別の場所を試す\n"
        "・もう目的が達成できているなら、その旨を assist で教えてね\n"
        "同じことの繰り返し以外で、次の手をJSONで出してください。"
    )


class Deduplicator:
    """実行済み行動を覚え、重複を判定する。直近履歴での反復ループも検知する。"""
    def __init__(self, history_size: int = 5, loop_threshold: int = 3):
        self._seen: set[str] = set()
        self._recent: list[str] = []          # 直近アクションの指紋（最大 history_size 件）
        self._history_size = history_size
        self._loop_threshold = loop_threshold

    def is_duplicate(self, action: dict) -> bool:
        return fingerprint(action) in self._seen

    def remember(self, action: dict) -> None:
        fp = fingerprint(action)
        self._seen.add(fp)
        self._recent.append(fp)
        if len(self._recent) > self._history_size:
            self._recent.pop(0)

    def note_attempt(self, action: dict) -> None:
        """実行されなくても「試みた」ことを直近履歴に刻む（重複連発のループ検知用）。"""
        fp = fingerprint(action)
        self._recent.append(fp)
        if len(self._recent) > self._history_size:
            self._recent.pop(0)

    def is_looping(self, action: dict) -> bool:
        """直近履歴で同じアクションが loop_threshold 回以上 → ループとみなす。"""
        fp = fingerprint(action)
        return self._recent.count(fp) >= self._loop_threshold

    def reset(self) -> None:
        self._seen.clear()
        self._recent.clear()


# ---- LLMの生出力（JSON文字列など）の繰り返し検知 ----
def _norm(text):
    return " ".join((text or "").split())


def explain_bad_json(err, same_as_before, contract=""):
    base = (
        "出した文がJSONのルールから外れているよ。\n"
        "JSONは『おもちゃ箱』みたいなもので、ふたの { から始めて } で閉じる、"
        "名前は 二重引用符 で囲む、というお約束があるんだ。\n"
        + f"今うまくいかなかった理由: {err}\n"
        + "・説明やあいさつは書かない（箱の中身だけ）\n"
        "・コードの ``` も付けない\n"
        "・最後の } まできちんと閉じる\n"
    )
    if same_as_before:
        base = (
            "あれ、さっきと“まったく同じ”文をもう一度出したね。\n"
            "同じやり方では同じところでつまずいてしまうよ。やり方を変えてみよう。\n"
        ) + base
    if contract:
        base += "この役割で正しい形はこれだよ → " + contract + "\n"
    return base + "もう一度、正しいJSONだけを出してください。"


class OutputTracker:
    def __init__(self):
        self._prev = None

    def is_repeat(self, text):
        return self._prev is not None and _norm(text) == self._prev

    def remember(self, text):
        self._prev = _norm(text)
