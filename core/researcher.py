# -*- coding: utf-8 -*-
#core/researcher.py — 情報収集・知識検索の役割
"""
計画の前提となる「知識・経験・攻撃状態」を集める役割。
長期記憶(LTM)・知識グラフ(KG)・関連教訓/スキル/ルールの取得を担う。
将来は role="explainer" などとは別に role を割り当てることもできる。
"""
from __future__ import annotations


class Researcher:
    """調査役。記憶/KG/経験から、現在の目的に関連する情報を集める。"""

    def __init__(self, ltm=None):
        self._ltm = ltm

    def bind(self, ltm) -> None:
        self._ltm = ltm

    # 関連知識（KGの1〜2ホップ探索）
    def related_knowledge(self, query: str, limit: int = 8) -> list[str]:
        if not self._ltm:
            return []
        try:
            return self._ltm.related_knowledge(query, limit=limit)
        except Exception:
            return []

    # 関連教訓（適応的関連度フィルタつき）
    def relevant_lessons(self, query: str, limit: int = 3) -> list[dict]:
        if not self._ltm:
            return []
        try:
            return self._ltm.relevant_lessons(query, limit=limit)
        except Exception:
            return []

    # 再利用できるスキル
    def relevant_skills(self, query: str, limit: int = 3) -> list[dict]:
        if not self._ltm:
            return []
        try:
            return self._ltm.relevant_skills(query, limit=limit)
        except Exception:
            return []

    # 自動適用ルール（経験学習の最上位）
    def active_rules(self, query: str, limit: int = 5) -> list[dict]:
        if not self._ltm:
            return []
        try:
            return self._ltm.relevant_rules(query, limit=limit)
        except Exception:
            return []

    # 攻撃状態（ペンテスト時の知識グラフ要約）
    def attack_state(self, host: str = "") -> str:
        if not self._ltm:
            return ""
        try:
            import pentest_kg
            return pentest_kg.summary_for_planner(self._ltm, host)
        except Exception:
            return ""

    # ===== Phase2: 仮説生成エンジン =====
    def analyze_observation(self, observation: str, world=None) -> dict:
        """観測を fact / assumption / hypothesis に分類する。
        - facts: テキストから決定論的に抽出した事実（推測ゼロ）
        - hypotheses: 事実に基づく検証可能な仮説（最低3件を目標）
        - assumptions: 事実未満の推測（あれば）
        返り値: {observation, facts, assumptions, hypotheses}"""
        from core import fact_layer
        facts = fact_layer.extract_facts(observation, source="observation")
        # 事実は世界状態へ保存（永続化）
        if world is not None:
            try:
                world.add_facts(facts)
            except Exception:
                pass
        hyps = self._generate_hypotheses(facts, observation, world)
        return {
            "observation": observation[:300],
            "facts": [f.to_dict() for f in facts],
            "assumptions": [],
            "hypotheses": [h.to_dict() for h in hyps],
        }

    def _generate_hypotheses(self, facts, observation: str, world=None) -> list:
        """抽出した事実から、検証可能な仮説を最低3件生成する。
        事実に直接ひもづく仮説のみを作る（存在しないサービスは仮説化しない）。"""
        from core.models import Hypothesis
        hyps = []
        services = [f for f in facts if f.type == "service"]
        ports = [f for f in facts if f.type == "port" and f.value == "open"]
        endpoints = [f for f in facts if f.type == "endpoint"]
        cves = [f for f in facts if f.type == "cve"]

        for s in services:
            label = f"{s.name}" + (f" {s.value}" if s.value else "")
            hyps.append(Hypothesis(
                description=f"{label} に既知のCVE脆弱性が存在する可能性",
                confidence=0.6 if s.value else 0.4,
                evidence=[f"観測事実: {label}"],
                next_steps=[f"searchsploit {s.name} {s.value}".strip(),
                            f"cve_lookup {s.name} {s.value}".strip()]))
            hyps.append(Hypothesis(
                description=f"{s.name} の設定不備（デフォルト設定/情報漏洩）",
                confidence=0.4,
                evidence=[f"観測事実: {label}"],
                next_steps=[f"{s.name} の設定・既定ページ・バナーを精査"]))
        for ep in endpoints:
            hyps.append(Hypothesis(
                description=f"エンドポイント {ep.name} に攻撃面がある可能性",
                confidence=0.5,
                evidence=[f"観測事実: {ep.name} (HTTP {ep.value})"],
                next_steps=[f"{ep.name} のパラメータ・認証・入力検証を調査"]))
        for c in cves:
            hyps.append(Hypothesis(
                description=f"{c.name} が悪用可能か検証",
                confidence=0.7,
                evidence=[f"観測事実: {c.name}"],
                next_steps=[f"{c.name} のPoC/exploitを調査して実証"]))

        # 事実が乏しく仮説が3件未満なら、観測の不足を埋める「次の偵察」仮説で補う
        generic = [
            Hypothesis(description="開いているポートに未列挙のサービスがある可能性",
                       confidence=0.4, evidence=[], next_steps=["より詳細なサービス列挙(-sV -sC)"]),
            Hypothesis(description="Web配下に未発見のディレクトリ/ファイルがある可能性",
                       confidence=0.4, evidence=[], next_steps=["gobuster/ffuf でコンテンツ探索"]),
            Hypothesis(description="既定/弱い認証情報が通る可能性",
                       confidence=0.3, evidence=[], next_steps=["既定資格情報の確認"]),
        ]
        i = 0
        while len(hyps) < 3 and i < len(generic):
            hyps.append(generic[i]); i += 1

        # 世界状態へ仮説を登録（永続）
        if world is not None:
            for h in hyps:
                try:
                    world.add_hypothesis(h.description, h.confidence,
                                         h.evidence, h.next_steps, h.status)
                except Exception:
                    pass
        return hyps
