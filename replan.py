# -*- coding: utf-8 -*-
#replan.py — 失敗分析 → 構造化された再計画指令
"""
失敗を「テキストヒント」でなく「型付きの再計画指令」に変える。
これにより planner は曖昧な反省文ではなく、明確な方針転換を受け取る。

指令の型:
  drop_approach   : 今のアプローチを捨てる（理由付き）
  try_alternative : 別の具体的手段を試す
  gather_more     : 情報不足 → 偵察に戻る
  change_target   : 対象/エンドポイントを変える

さらに、失敗から得た教訓を *その場で* LTM に保存する（セッション内学習）。
次の同種objectiveで relevant_lessons として即座に引かれる。
"""
from __future__ import annotations

import re


# 失敗結果の分類 → 指令タイプと助言テンプレート
_PATTERNS = [
    (["403", "forbidden", "denied", "権限", "unauthorized", "401"],
     "try_alternative",
     "アクセスが拒否された。認証/権限の回避（別の認証情報・トークン改ざん・別経路）を試す。"),
    (["404", "not found", "見つから", "no such"],
     "change_target",
     "対象が見つからない。エンドポイント/パス/ポートを変える。偵察で正しい対象を特定する。"),
    (["timeout", "timed out", "接続でき", "connection refused", "unreachable"],
     "change_target",
     "接続できない。対象のホスト/ポート/プロトコルを見直す。"),
    (["syntax", "invalid", "parse", "構文", "不正な"],
     "try_alternative",
     "構文/形式が不正。ペイロードやコマンドの形式を修正して別の書き方で試す。"),
    (["empty", "空", "no output", "何も", "失敗"],
     "gather_more",
     "有効な反応が得られない。偵察に戻って対象の挙動を観察し直す。"),
]


def analyze(result: str, objective: str = "", attempted: str = "") -> dict:
    """失敗結果を分類して構造化指令を返す。
    返り値: {directive, advice, signal, replan_hint}"""
    low = (result or "").lower()
    for kws, directive, advice in _PATTERNS:
        if any(k in low for k in kws):
            return {
                "directive": directive,
                "advice": advice,
                "signal": next(k for k in kws if k in low),
                # plannerに添えるヒント（型を明示）
                "replan_hint": f"【再計画指令: {directive}】{advice}"
                               + (f" 直前に試して失敗した手: {attempted[:80]}"
                                  if attempted else ""),
            }
    # 分類不能 → 汎用の方針転換
    return {
        "directive": "drop_approach",
        "advice": "現在のアプローチが機能していない。別の角度から攻める。",
        "signal": "unknown",
        "replan_hint": "【再計画指令: drop_approach】今の手は機能していない。"
                       "同じ手を繰り返さず、別の脆弱性・別の手段に切り替える。"
                       + (f" 失敗した手: {attempted[:80]}" if attempted else ""),
    }


def lesson_from_failure(objective: str, attempted: str, result: str) -> str:
    """失敗から、次回に活きる簡潔な教訓文を作る（セッション内即時保存用）。"""
    a = analyze(result, objective, attempted)
    short_attempt = re.sub(r"\s+", " ", attempted)[:60]
    return (f"目的「{objective[:40]}」で「{short_attempt}」は失敗"
            f"（{a['signal']}）。{a['advice']}")


def record_immediate_lesson(ltm, objective: str, attempted: str,
                            result: str, goal: str = "") -> None:
    """失敗教訓をその場でLTMへ保存する。次の同種objに即反映される。
    scoreは低め(-1)にして「避けるべき手」として記録。"""
    try:
        lesson = lesson_from_failure(objective, attempted, result)
        ltm.add_lesson(goal or objective, lesson, score=-1)
    except Exception:
        pass
