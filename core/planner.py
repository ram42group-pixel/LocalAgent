# -*- coding: utf-8 -*-
#core/planner.py — 計画立案の役割（Goal/Objective/Task/Action）
"""
計画に関する責務をまとめる役割クラス。
ロジックは既存 agent_loop の関数へ委譲する（再実装しない）ため、
挙動は従来と完全に同じ。将来は role="plan" のモデルを差し替えるだけで
Planner だけ別LLM（例: Gemini）に切り替えられる。
"""
from __future__ import annotations
from core.models import Goal


class Planner:
    """計画立案役。goal分解／次アクション立案／手順分解を担う。"""

    def __init__(self, agent_loop_module=None):
        # 既存ロジックを持つ agent_loop を遅延importして委譲先にする
        if agent_loop_module is None:
            import agent_loop as agent_loop_module
        self._al = agent_loop_module

    # --- Goal層: 要望をゴール/目的/タグへ分解 ---
    def make_goal(self, request: str) -> Goal:
        gd = self._al.make_goal(request)            # 既存: dictを返す
        return Goal.from_goal_dict(request, gd)

    def additional_objectives(self, request: str, prev_goal: str,
                              tags: list) -> list:
        return self._al._additional_objectives(request, prev_goal, tags)

    # --- Task層: objectiveを手順(タスク)へ分解（大規模タスク対応） ---
    def plan_steps(self, stm, ltm) -> list:
        return self._al.plan_steps(stm, ltm)

    # --- Action層: 次の1手を立案（planner+critic討論つき） ---
    def next_action(self, stm, related: list, feedback: str = "") -> dict:
        return self._al.plan_with_debate(stm, related, feedback)

    # 空アクション判定（空プランループ防止に使う）
    def is_empty_action(self, action: dict) -> bool:
        return self._al._is_empty_action(action)
