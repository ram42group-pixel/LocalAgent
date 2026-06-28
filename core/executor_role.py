# -*- coding: utf-8 -*-
#core/executor_role.py — アクション実行の役割
"""
アクション実行の責務をまとめる役割。既存 executor.run_action へ委譲する。
（ファイル名を executor_role.py としているのは、既存 executor.py との衝突回避。）
"""
from __future__ import annotations


class ExecutorRole:
    """実行役。1アクションを実行して結果文字列を返す。"""

    def __init__(self, agent_loop_module=None):
        if agent_loop_module is None:
            import agent_loop as agent_loop_module
        self._al = agent_loop_module

    def execute(self, action: dict) -> str:
        # 既存の execute_action（承認・dry-run・dispatchを内包）へ委譲
        return self._al.execute_action(action)

    def is_failure(self, result: str) -> bool:
        try:
            from memory.short_term import is_failure_result
            return is_failure_result(result)
        except Exception:
            return False
