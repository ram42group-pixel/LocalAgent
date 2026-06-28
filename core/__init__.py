# -*- coding: utf-8 -*-
#core/__init__.py — Mythos系アーキテクチャのコアパッケージ
"""
LocalAgent を「役割(role)ごとの責務分離」アーキテクチャへ進化させるコア。

agent_loop.py に集中していた責務を、以下の役割クラスへ分離する:
  Planner       — 計画立案（goal/objective/task/action の各層）
  Researcher    — 情報収集・知識検索（記憶/KG/関連経験の取得）
  Executor      — アクション実行（既存 executor のラッパ）
  Critic        — 実行後の成否判定・原因分析・改善案生成
  MemoryManager — 記憶の読み書き・経験/教訓/ルールの抽象化

これらは既存の関数（agent_loop内）やモジュール（memory/executor等）へ
委譲する“ファサード”であり、ロジックを再実装しない。
そのため既存機能は一切壊れず、構造だけがクリーンになる。

将来のマルチエージェント化（役割ごとに別モデル）は、各役割クラスが
providers の role ルーティングを使うことで自然に実現される。
"""

from core.models import (Goal, Objective, Task, Action, Experience, Lesson, Rule,
                         Fact, Assumption, Hypothesis)
from core.planner import Planner
from core.researcher import Researcher
from core.executor_role import ExecutorRole
from core.critic import Critic
from core.memory_manager import MemoryManager
from core import roles as roles
from core import fact_layer
from core import hallucination_guard
from core import target_resolver
from core import target_manager
from core import execution_guard
from core import evidence_engine
from core import decision_provenance
from core import decision_replay
from core import loop_detector
from core.decision_provenance import DecisionProvenance
from core.loop_detector import LoopDetector
from core.world_state import WorldState
from core.exploration_engine import ExplorationEngine, infer_category
from core.strategy_engine import StrategyEngine

# マルチエージェント用の役割(researcher/coder/security)をルーティングへ登録
try:
    roles.ensure_roles_registered()
except Exception:
    pass

__all__ = [
    "Goal", "Objective", "Task", "Action",
    "Experience", "Lesson", "Rule",
    "Fact", "Assumption", "Hypothesis",
    "Planner", "Researcher", "ExecutorRole", "Critic", "MemoryManager",
    "WorldState", "fact_layer", "hallucination_guard", "roles",
    "ExplorationEngine", "StrategyEngine", "infer_category",
    "target_resolver", "target_manager", "execution_guard", "evidence_engine",
    "decision_provenance", "decision_replay", "loop_detector",
    "DecisionProvenance", "LoopDetector",
]
