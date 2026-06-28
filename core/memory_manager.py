# -*- coding: utf-8 -*-
#core/memory_manager.py — 記憶管理と経験学習の抽象化の役割
"""
記憶の読み書きと、経験→教訓→ルールの抽象化を担う役割。
既存 LongTermMemory / consolidation / reflect_and_learn へ委譲しつつ、
Experience→Lesson→Rule の昇華パイプラインを提供する。

経験学習の流れ:
  Critic が作った Experience（失敗/成功の生データ）
    → record_experience() で保存
    → distill_rule() で「失敗の教訓」を Rule（自動適用ルール）へ昇華
    → 次回以降 Researcher.active_rules() が拾い、Planner の文脈へ自動注入
"""
from __future__ import annotations
from core.models import Experience, Lesson, Rule


class MemoryManager:
    """記憶役。経験/教訓/ルールの保存・昇華・整理を担う。"""

    def __init__(self, ltm=None, agent_loop_module=None):
        self._ltm = ltm
        if agent_loop_module is None:
            import agent_loop as agent_loop_module
        self._al = agent_loop_module

    def bind(self, ltm) -> None:
        self._ltm = ltm

    # --- 経験の保存 ---
    def record_experience(self, exp: Experience) -> None:
        """1試行の経験を保存。失敗経験は即座にルール化も試みる。"""
        if not self._ltm:
            return
        try:
            self._ltm.add_experience(
                objective=exp.objective,
                action=exp.action, result=exp.result, success=exp.success)
        except Exception:
            pass

    # --- 批評結果（改善案）を教訓として保存 ---
    def record_critique(self, objective: str, goal: str, critique: dict) -> None:
        """Criticの分析（原因・改善案）を教訓として長期記憶へ。"""
        if not self._ltm:
            return
        try:
            if not critique.get("success"):
                lesson = (f"失敗: {critique.get('cause','')[:80]} "
                          f"→ 改善: {critique.get('improvement','')[:80]}")
                self._ltm.add_lesson(goal or objective, lesson, score=-1)
                # 失敗教訓をルールへ昇華（次回自動適用）
                self.distill_rule(objective, critique)
            else:
                # 成功も軽い教訓として残す（強化）
                if critique.get("improvement"):
                    self._ltm.add_lesson(goal or objective,
                                         f"有効だった手: {critique['improvement'][:80]}",
                                         score=1)
        except Exception:
            pass

    # --- 教訓 → ルール への昇華 ---
    def distill_rule(self, objective: str, critique: dict) -> None:
        """失敗の批評から自動適用ルールを作る。
        例: Flask起動失敗 → 「appオブジェクトを確認してから起動する」"""
        if not self._ltm:
            return
        directive = critique.get("improvement") or critique.get("cause")
        if not directive:
            return
        try:
            self._ltm.add_rule(condition=objective[:60],
                               directive=directive[:120],
                               source_lesson=critique.get("cause", "")[:80])
        except Exception:
            pass

    # --- objective終了時の振り返り（既存ロジックへ委譲） ---
    def reflect(self, stm, done: bool) -> None:
        try:
            self._al.reflect_and_learn(stm, self._ltm, done)
        except Exception:
            pass

    # --- スキル生成（既存ロジックへ委譲） ---
    def generate_skill(self, stm) -> None:
        try:
            self._al.generate_skill(stm, self._ltm)
        except Exception:
            pass

    # --- 記憶の保存（goal/objective単位のサマリ） ---
    def save_objective(self, goal: str, objective: str, summary: str,
                       tags: list, success: bool) -> None:
        if not self._ltm:
            return
        try:
            self._ltm.save(goal=goal, objective=objective, summary=summary,
                           tags=tags, success=success)
        except Exception:
            pass

    # --- 定期的な記憶の統合・蒸留・剪定（Reflectionループ） ---
    def consolidate(self) -> dict:
        if not self._ltm:
            return {}
        try:
            import consolidation
            return consolidation.run_full(self._ltm)
        except Exception:
            return {}

    # --- Reflectionループ: 最近の失敗/成功/頻出手法を分析して自己改善 ---
    def reflection_loop(self) -> dict:
        """最近の経験を振り返り、失敗パターンを新ルールへ昇華し、
        効果の低いルールを抑制、よく効くルールを強化する。記憶も統合する。
        返り値: {failures, successes, new_rules, weak_rules, strong_rules, consolidated}"""
        if not self._ltm:
            return {}
        out = {"failures": 0, "successes": 0, "new_rules": 0,
               "weak_rules": 0, "strong_rules": 0}
        try:
            recent = self._ltm.recent_experiences(limit=30)
            fails = [e for e in recent if not e.get("success")]
            succ = [e for e in recent if e.get("success")]
            out["failures"] = len(fails)
            out["successes"] = len(succ)
            # 繰り返し失敗している目的を抽出し、ルール化（同じ轍を踏まない）
            from collections import Counter
            fail_objs = Counter(e.get("objective", "")[:60] for e in fails)
            for obj, cnt in fail_objs.items():
                if cnt >= 2 and obj:
                    self._ltm.add_rule(
                        condition=obj,
                        directive="この種の目的は過去に複数回失敗。前回と違う手段を最優先で試す",
                        source_lesson=f"{cnt}回の失敗を観測")
                    out["new_rules"] += 1
        except Exception:
            pass
        # ルールの効果分析: 効果の低い/高いルールを集計（priority/confidenceで識別）
        try:
            for r in self._ltm.all_rules(limit=200):
                conf = r.get("confidence")
                uses = r.get("uses", 0) or 0
                if uses >= 3 and conf is not None:
                    if conf < 0.35:
                        out["weak_rules"] += 1   # 低信頼=自動適用から外れる（relevant_rulesで除外済）
                    elif conf >= 0.7:
                        out["strong_rules"] += 1
        except Exception:
            pass
        # Phase3: 戦略(Strategy)の評価。成功率の高い戦略を強化、低い戦略を弱体化。
        try:
            strats = self._ltm.all_strategies()
            out["strong_strategies"] = [s["name"] for s in strats
                                        if (s.get("uses", 0) or 0) >= 3
                                        and (s.get("success_rate", 0) or 0) >= 0.6]
            out["weak_strategies"] = [s["name"] for s in strats
                                      if (s.get("uses", 0) or 0) >= 3
                                      and (s.get("success_rate", 0) or 0) < 0.3]
            out["exploration"] = self._ltm.exploration_summary()
        except Exception:
            pass
        # 記憶の統合（重複削除・教訓→スキル昇華・剪定）
        out["consolidated"] = self.consolidate()
        return out

    def target_reflection(self, world) -> dict:
        """Phase4.1: ターゲット拡張と却下の統計を分析し、教訓化する。
        - Target Expansion 成功（trusted昇格）の源(source)別集計
        - Rejected Targets（許可外アクセス試行）の集計
        - 誤検出・繰り返す却下は教訓/ルールへ
        返り値: {expanded, rejected, sources, ...}"""
        out = {"expanded": 0, "rejected": 0, "sources": {}, "relations": {},
               "chains": []}
        if world is None:
            return out
        try:
            trusted = world.trusted_targets()
            # user/DNS起点は除いた「拡張で得たターゲット」を数える
            expanded = [t for t in trusted
                        if t.get("source") not in ("user", "DNS Lookup")]
            out["expanded"] = len(expanded)
            from collections import Counter
            src = Counter(t.get("source", "") for t in expanded)
            out["sources"] = dict(src)
            rejected = world.rejected_targets()
            out["rejected"] = len(rejected)

            # Phase4.2: Evidence Chain の有効性を評価する。
            # relation(関係)別に、到達可能な探索につながったかを集計。
            graph = world.target_graph()
            rel_useful = Counter()    # 到達可能ノードを生んだ関係
            rel_dead = Counter()      # rejected/到達不能を生んだ関係
            for n in graph:
                rel = n.get("relation") or n.get("source") or "?"
                if rel in ("root", ""):
                    continue
                if n.get("status") == "rejected":
                    rel_dead[rel] += 1
                elif world.is_reachable(n["target"]):
                    rel_useful[rel] += 1
                else:
                    rel_dead[rel] += 1
            out["relations"] = {
                "useful": dict(rel_useful), "ineffective": dict(rel_dead)}
            # 到達可能な拡張ターゲットの証拠経路を記録（説明可能性）
            for t in expanded:
                ch = world.chain_explanation(t["target"])
                if ch and "経路なし" not in ch:
                    out["chains"].append(ch)

            # 誤誘導が多い関係（ineffective>>useful）は教訓化して信頼を下げる
            for rel, dead in rel_dead.items():
                good = rel_useful.get(rel, 0)
                if dead >= 2 and dead > good and self._ltm:
                    self._ltm.add_rule(
                        condition=f"関係 {rel} で発見したターゲット",
                        directive=(f"{rel} 由来のターゲットは誤誘導が多い"
                                   f"（無効{dead}/有効{good}）。確証を高めてから対象にする"),
                        source_lesson=f"{rel}: 無効{dead}件")

            # 同じ許可外ホストへ繰り返しアクセスしようとした → 教訓化
            rc = Counter(rejected)
            for host, cnt in rc.items():
                if cnt >= 2 and host and self._ltm:
                    self._ltm.add_rule(
                        condition=f"ターゲット {host} へのアクセス",
                        directive=(f"{host} は Root からの証拠経路が無い。"
                                   "証拠経路(Evidence Chain)を確立してから対象にすること"),
                        source_lesson=f"{cnt}回の許可外アクセス却下")
        except Exception:
            pass
        return out
