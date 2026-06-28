# -*- coding: utf-8 -*-
#router.py — 動的ルーティング（タスク要求ベクトル × モデル能力ベクトル）
"""
役割固定(ROLE_ROUTES)・ツール固定(experts)に加え、
*その時のタスクが何を要求するか* に応じて最適モデルを動的に選ぶ。

流れ:
  planner が現objectiveを分類 → task_vector(要求trait重み) を作る
  → capabilities.rank_for_needs で能力ベクトル最上位を選ぶ
  → そのモデルで実行

これにより「security+tool_usageを要求する目的にはセキュリティ特化モデル、
reasoningを要求する計画にはreasoning強モデル」が自動で当たる。
"""
from __future__ import annotations

import capabilities


# 役割ごとの“素の”要求trait（タスク分類が無い時のフォールバック）
ROLE_NEEDS = {
    "plan":    {"planning": 2, "reasoning": 2, "speed": 0.5},
    "goal":    {"reasoning": 2, "planning": 1},
    "system":  {"planning": 2, "tool_usage": 1.5, "reasoning": 1},
    "judge":   {"reasoning": 2, "reflection": 1},
    "summary": {"reasoning": 1, "speed": 1},
    "narrator": {"speed": 2, "reasoning": 0.5},
    "explainer": {"reasoning": 1.5, "speed": 1},
    "reflect": {"reflection": 2, "reasoning": 1.5},
    "steps":   {"planning": 2, "reasoning": 1},
    "critic":  {"reasoning": 2, "reflection": 1.5},
}

# ツールごとの要求trait（旧 model_assign.TOOL_NEEDS を能力ベクトル次元に移植）
TOOL_NEEDS = {
    "exploit_run": {"security": 2.5, "tool_usage": 1.5, "refusal_rate": 1.5},
    "privesc":     {"security": 2, "tool_usage": 1.5, "refusal_rate": 1.5},
    "lateral":     {"security": 2, "planning": 1, "refusal_rate": 1},
    "metasploit":  {"security": 2, "tool_usage": 2, "refusal_rate": 1.5},
    "sqlmap":      {"security": 2, "tool_usage": 1.5, "refusal_rate": 1},
    "strategize":  {"planning": 2, "reasoning": 2},
    "web_scan":    {"tool_usage": 2, "security": 1},
    "recon":       {"tool_usage": 1.5, "reasoning": 1},
    "report":      {"reasoning": 2, "speed": 1},
    "vision":      {"reasoning": 1, "tool_usage": 1},
}


def best_for_role(role: str, candidates: list[str] = None) -> str:
    """役割に最適なモデルキーを能力ベクトルから選ぶ（割り当て用）。空なら""。"""
    needs = ROLE_NEEDS.get(role, {"reasoning": 1})
    ranked = capabilities.rank_for_needs(needs, candidates)
    return ranked[0][0] if ranked and ranked[0][1] > 0 else ""


def best_for_tool(tool: str, candidates: list[str] = None) -> str:
    """ツールに最適なモデルキーを能力ベクトルから選ぶ。空なら""。"""
    needs = TOOL_NEEDS.get(tool, {"tool_usage": 1})
    ranked = capabilities.rank_for_needs(needs, candidates)
    return ranked[0][0] if ranked and ranked[0][1] > 0 else ""


def assign_all(candidates: list[str] = None) -> dict:
    """全役割・全ツールへ能力ベクトルから一括割り当て（旧 assign_from_bench 相当）。
    返り値: {roles: {role: key}, tools: {tool: key}}（key=provider/model）"""
    cands = candidates or list(capabilities.all_vectors().keys())
    roles = {r: best_for_role(r, cands) for r in ROLE_NEEDS}
    tools = {t: best_for_tool(t, cands) for t in TOOL_NEEDS}
    return {"roles": roles, "tools": tools}

# タスク内容のキーワード → 要求traitの加重（タスク分類）
_TASK_SIGNALS = {
    "security":   (["脆弱", "exploit", "sqli", "xss", "侵入", "攻撃", "vuln",
                    "inject", "traversal", "rce", "privesc", "ペネトレ", "cve"],
                   {"security": 2, "tool_usage": 1.5, "refusal_rate": 1.5}),
    "tool":       (["コマンド", "実行", "scan", "ツール", "http", "curl", "nmap",
                    "tool", "command"],
                   {"tool_usage": 2, "planning": 1}),
    "reasoning":  (["分析", "推論", "理由", "なぜ", "考察", "判断", "比較"],
                   {"reasoning": 2, "reflection": 1}),
    "planning":   (["計画", "手順", "戦略", "段取り", "plan", "step", "次の手"],
                   {"planning": 2, "reasoning": 1}),
}


def classify_task(text: str) -> dict:
    """目的テキストを分類して要求traitベクトルを作る。
    複数シグナルが当たれば加算合成。何も当たらなければ空（役割既定にフォールバック）。"""
    low = (text or "").lower()
    needs = {}
    for _, (kws, traits) in _TASK_SIGNALS.items():
        if any(k.lower() in low for k in kws):
            for t, w in traits.items():
                needs[t] = needs.get(t, 0) + w
    return needs


def route(role: str, task_text: str = "",
          candidates: list[str] = None) -> tuple | None:
    """役割＋タスク内容から最適な (provider, model) を選ぶ。
    candidates: 能力ベクトルを持つキーの中で選ぶ（Noneなら全モデル）。
    返り値: (provider, model) または None（能力データ無し時）。"""
    needs = dict(ROLE_NEEDS.get(role, {"reasoning": 1}))
    # タスク分類を上乗せ（動的部分）
    for t, w in classify_task(task_text).items():
        needs[t] = needs.get(t, 0) + w
    ranked = capabilities.rank_for_needs(needs, candidates)
    # 能力データが無い/全ゼロなら None（呼び出し側が従来のROLE_ROUTESを使う）
    if not ranked or ranked[0][1] <= 0.0:
        return None
    key = ranked[0][0]
    prov, _, mdl = key.partition("/")
    return (prov, mdl) if mdl else None


def explain(role: str, task_text: str = "",
            candidates: list[str] = None) -> dict:
    """ルーティング判断の説明（UIやログ用）。"""
    needs = dict(ROLE_NEEDS.get(role, {}))
    for t, w in classify_task(task_text).items():
        needs[t] = needs.get(t, 0) + w
    ranked = capabilities.rank_for_needs(needs, candidates)
    return {"role": role, "needs": needs, "ranking": ranked[:5]}
