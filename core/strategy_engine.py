# -*- coding: utf-8 -*-
#core/strategy_engine.py — 戦略エンジン（Phase3）
"""
Ruleの上位概念=Strategy（戦略）を管理する。

階層:
  Rule     個別の行動規範（例: Apache検出時はCVE調査）
  Policy   条件付きの方針（例: バージョン情報があれば既知脆弱性を優先）
  Strategy 複数Ruleを束ねた探索方針（例: 既知脆弱性優先 → 不発なら設定/認証/uploadへ）

Strategyは成功率を持ち、Reflectionで強化/弱体化される。
成果が出ない戦略から、別カテゴリ探索へ切り替える判断材料になる。

永続化は LongTermMemory の strategies テーブル（別途追加）を使う。
DBが無い場合も既定戦略でフォールバック動作する。
"""
from __future__ import annotations


# 既定の戦略カタログ（探索の大方針）。各戦略は探索カテゴリの優先順を持つ。
DEFAULT_STRATEGIES = [
    {"name": "known_cve_first",
     "description": "バージョン判明時は既知脆弱性(CVE)を最優先",
     "category_order": ["cve", "config", "auth", "upload", "api", "session", "backup", "content"]},
    {"name": "web_surface_first",
     "description": "Web攻撃面（コンテンツ列挙→認証→アップロード）を優先",
     "category_order": ["content", "auth", "upload", "api", "session", "config", "cve", "backup"]},
    {"name": "misconfig_first",
     "description": "設定不備・情報漏洩・バックアップ等の容易な穴を優先",
     "category_order": ["config", "backup", "content", "auth", "api", "upload", "session", "cve"]},
    {"name": "auth_first",
     "description": "認証・セッション周りの弱点を優先",
     "category_order": ["auth", "session", "api", "config", "content", "upload", "cve", "backup"]},
]


class StrategyEngine:
    """戦略の選択・切替・成否記録を担う。"""

    def __init__(self, ltm=None):
        self._ltm = ltm
        self._catalog = {s["name"]: dict(s) for s in DEFAULT_STRATEGIES}
        self._current = None
        # DBから成功率を読み込んで反映
        self._load_stats()

    def _load_stats(self) -> None:
        if not self._ltm:
            return
        try:
            for r in self._ltm.all_strategies():
                if r["name"] in self._catalog:
                    self._catalog[r["name"]]["success_rate"] = r.get("success_rate", 0.0)
                    self._catalog[r["name"]]["uses"] = r.get("uses", 0)
        except Exception:
            pass

    def current(self) -> dict:
        if self._current is None:
            self._current = self.best_strategy()
        return self._current

    def best_strategy(self) -> dict:
        """成功率が最も高い戦略を選ぶ（未経験は中庸の0.5扱いで探索を促す）。"""
        def keyf(s):
            sr = s.get("success_rate")
            uses = s.get("uses", 0) or 0
            # 未経験戦略は探索のため少し高めに（楽観的初期化）
            return (sr if sr is not None and uses >= 2 else 0.55)
        best = max(self._catalog.values(), key=keyf)
        return best

    def category_priority(self) -> list:
        """現在戦略のカテゴリ優先順を返す（Exploration Engineの並べ替えに使う）。"""
        return list(self.current().get("category_order", []))

    def switch_strategy(self, avoid: str = "") -> dict:
        """現在の戦略から別の戦略へ切り替える（成果が出ない時）。
        avoid と異なる、次に成功率の高い戦略を選ぶ。"""
        cur_name = (self._current or {}).get("name", "")
        avoid_names = {cur_name}
        if avoid:
            avoid_names.add(avoid)
        candidates = [s for n, s in self._catalog.items() if n not in avoid_names]
        if not candidates:
            return self.current()

        def keyf(s):
            sr = s.get("success_rate")
            uses = s.get("uses", 0) or 0
            return (sr if sr is not None and uses >= 2 else 0.55)
        self._current = max(candidates, key=keyf)
        return self._current

    def record_outcome(self, success: bool) -> None:
        """現在戦略の成否を記録（成功率を更新・永続化）。"""
        if not self._ltm or self._current is None:
            return
        try:
            self._ltm.record_strategy_outcome(self._current["name"], success)
            self._load_stats()
        except Exception:
            pass

    def reorder_hypotheses(self, hypotheses: list[dict]) -> list[dict]:
        """現在戦略のカテゴリ優先順で仮説を並べ替える（探索の方向づけ）。"""
        from core.exploration_engine import infer_category
        order = self.category_priority()
        rank = {c: i for i, c in enumerate(order)}

        def keyf(h):
            cat = h.get("category") or infer_category(h.get("description", ""))
            return rank.get(cat, len(order))
        return sorted(hypotheses, key=keyf)

    def summary(self) -> str:
        cur = self.current()
        return (f"戦略={cur['name']}（{cur['description']}）"
                f" 成功率={cur.get('success_rate','未測定')}")
