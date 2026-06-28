# -*- coding: utf-8 -*-
#core/models.py — Mythos系の階層データモデル
"""
目標の階層化と、経験学習の抽象化に使う軽量データクラス群。

階層:  Goal → Objective → Task → Action
  Goal      ユーザー要望の最上位の達成目標
  Objective ゴールを構成する中位の目的（複数）
  Task      objectiveを構成する具体的な作業単位（大規模タスク分解用）
  Action    実際にexecutorへ渡す1手（command/tool/code/file/assist）

経験学習:  Experience → Lesson → Rule
  Experience 1回の試行の生データ（行動・結果・成否）
  Lesson     experienceから抽出した教訓（再利用可能な気づき）
  Rule       lessonを昇華した自動適用ルール（次回計画へ自動注入）

dataclassのみで外部依存なし。既存のdict表現とも相互変換できる
（to_dict/from_dict）ので、既存コード（dictで動く）と無理なく繋がる。
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import time


# ====== 目標階層 ======

@dataclass
class Action:
    """executorへ渡す最小実行単位。既存のaction dictと相互変換可能。"""
    type: str = ""                 # command/tool/code/file/assist/web_search
    payload: dict = field(default_factory=dict)   # command/name/args/code/path等
    reason: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Action":
        d = dict(d or {})
        t = d.pop("type", "")
        reason = d.pop("reason", "")
        return cls(type=t, payload=d, reason=reason)

    def to_dict(self) -> dict:
        """既存executorが期待する平坦なdictへ戻す。"""
        out = {"type": self.type, "reason": self.reason}
        out.update(self.payload or {})
        return out


@dataclass
class Task:
    """objectiveを構成する作業単位。大規模タスクの中間分解層。"""
    description: str
    status: str = "pending"        # pending/active/done/failed
    actions: list[Action] = field(default_factory=list)
    result: str = ""

    def to_dict(self) -> dict:
        return {"description": self.description, "status": self.status,
                "result": self.result,
                "actions": [a.to_dict() for a in self.actions]}


@dataclass
class Objective:
    """ゴールを構成する中位の目的。既存のstm.objectives(文字列)を包含。"""
    description: str
    status: str = "pending"
    tasks: list[Task] = field(default_factory=list)
    summary: str = ""
    success: bool = False

    def to_dict(self) -> dict:
        return {"description": self.description, "status": self.status,
                "summary": self.summary, "success": self.success,
                "tasks": [t.to_dict() for t in self.tasks]}


@dataclass
class Goal:
    """ユーザー要望の最上位達成目標。"""
    request: str
    title: str = ""
    objectives: list[Objective] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"request": self.request, "title": self.title,
                "tags": list(self.tags),
                "objectives": [o.to_dict() for o in self.objectives]}

    @classmethod
    def from_goal_dict(cls, request: str, gd: dict) -> "Goal":
        """既存 make_goal() の戻り(dict)から Goal を組み立てる。"""
        gd = gd or {}
        objs = [Objective(description=str(o))
                for o in (gd.get("objectives") or [])]
        return cls(request=request, title=gd.get("goal", ""),
                   objectives=objs, tags=list(gd.get("tags") or []))


# ====== 仮説駆動（Phase2）: 事実・推測・仮説 ======

@dataclass
class Fact:
    """観測から抽出した検証済みの事実。World Stateに保存される。"""
    type: str                      # service/version/port/os/endpoint/credential 等
    name: str = ""
    value: str = ""                # version文字列など
    confidence: float = 1.0        # 観測由来は1.0、推定混じりは低く
    source: str = ""               # どの観測から得たか

    def to_dict(self) -> dict:
        return asdict(self)

    def key(self) -> str:
        return f"{self.type}:{self.name}:{self.value}".lower()


@dataclass
class Assumption:
    """未検証の推測。事実として扱ってはならない。"""
    statement: str
    confidence: float = 0.3
    basis: str = ""                # 何を根拠にした推測か

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Hypothesis:
    """検証可能な仮説。Investigationの対象になる。"""
    description: str
    confidence: float = 0.0
    evidence: list = field(default_factory=list)     # 支持する観測/事実
    next_steps: list = field(default_factory=list)   # 検証のための手
    status: str = "open"           # open/testing/confirmed/refuted
    category: str = ""             # cve/config/upload/auth/session/api/recon等（多様性管理用）

    def to_dict(self) -> dict:
        return asdict(self)


# ====== 経験学習の抽象化 ======

@dataclass
class Experience:
    """1試行の生データ。Critic/MemoryManagerが教訓化の素材に使う。"""
    objective: str
    action: dict
    result: str
    success: bool
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Lesson:
    """experienceから抽出した再利用可能な教訓。"""
    context: str                   # どんな状況で
    insight: str                   # 何を学んだか
    score: int = 0                 # 正(成功由来)/負(失敗由来)の強さ
    source_objective: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Rule:
    """lessonを昇華した自動適用ルール。次回計画へ自動注入される。"""
    condition: str                 # 適用条件（いつ）
    directive: str                 # 守るべき指示（何を）
    weight: float = 1.0            # 信頼度（適用回数や成功率で増減）
    uses: int = 0
    source_lesson: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def as_hint(self) -> str:
        """planner へ注入する1行ヒント表現。"""
        return f"[ルール] {self.condition} のときは: {self.directive}"
