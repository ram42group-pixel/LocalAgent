# -*- coding: utf-8 -*-
#core/critic.py — 実行後の批評（成否判定・原因分析・改善案）の役割
"""
各アクション実行後に動く批評役。
  - 成功判定（結果が失敗パターンか）
  - 原因分析（なぜ失敗/成功したか。既存 replan.analyze を活用）
  - 改善案（次に何をすべきか）
批評結果は MemoryManager 経由で長期記憶へ保存され、経験学習の素材になる。

目的単位の達成判定は既存 is_objective_done（judge役LLM）へ委譲する。
"""
from __future__ import annotations
from core.models import Experience


class Critic:
    """批評役。アクション結果を判定・分析し、改善のヒントを返す。"""

    def __init__(self, agent_loop_module=None):
        if agent_loop_module is None:
            import agent_loop as agent_loop_module
        self._al = agent_loop_module

    # --- アクション単位の批評 ---
    def critique(self, objective: str, action: dict, result: str,
                 goal: str = "") -> dict:
        """1アクションの実行結果を批評する。
        返り値: {success, cause, improvement, directive}"""
        try:
            from memory.short_term import is_failure_result
            failed = is_failure_result(result)
        except Exception:
            failed = False
        success = not failed

        cause = ""
        improvement = ""
        directive = ""
        if failed:
            # 既存の構造化再計画ロジックで原因分析＋改善方針を得る
            try:
                import replan
                attempted = (action.get("command") or action.get("url")
                             or action.get("name") or "") if isinstance(action, dict) else ""
                d = replan.analyze(result, objective, attempted)
                cause = d.get("advice", "") or d.get("reason", "")
                improvement = d.get("replan_hint", "")
                directive = d.get("directive", "")
            except Exception:
                cause = "失敗（原因分析を取得できず）"
                improvement = "別のアプローチを試す"
        else:
            improvement = "この手は有効。次の段階へ進む"

        return {"success": success, "cause": cause,
                "improvement": improvement, "directive": directive}

    def to_experience(self, objective: str, action: dict, result: str,
                      success: bool) -> Experience:
        return Experience(objective=objective, action=action,
                          result=result, success=success)

    # --- 目的単位の達成判定（judge役LLMへ委譲） ---
    def objective_done(self, stm) -> bool:
        return self._al.is_objective_done(stm)
