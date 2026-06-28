# -*- coding: utf-8 -*-
#tools/strategy_tool.py — 攻撃戦略の立案ツール（ReAct的な複数仮説検討）
"""
現在の攻撃グラフから複数の攻撃仮説を生成・採点し、最有望の経路を提示する。
線形に1手ずつ考えるのでなく、分岐を並行検討して有望な枝を選ぶ。
"""
from __future__ import annotations

from tools.base import Tool


class StrategizeTool(Tool):
    name = "strategize"
    description = ("現在の攻撃グラフから複数の攻撃仮説を立て、成功可能性×インパクト÷コストで"
                   "採点・順位付けし、次に取るべき最有望の経路を提示する（ReAct的な戦略立案）")
    args = {"objective": "達成したい目的（例: 10.0.0.5への初期侵入）",
            "n": "生成する仮説の数（既定3）"}

    def run(self, args: dict) -> str:
        objective = str(args.get("objective", "")).strip()
        if not objective:
            return "エラー: objective を指定してください"
        try:
            n = int(args.get("n", 3))
        except Exception:
            n = 3
        import strategist
        res = strategist.deliberate(objective, n=max(2, min(n, 5)))
        hyps = res.get("hypotheses", [])
        if not hyps:
            return "戦略を立案できませんでした（攻撃グラフに情報が少ない可能性）。まず偵察を進めてください。"
        out = [f"== 攻撃戦略: {objective} =="]
        for i, h in enumerate(hyps, 1):
            out.append(
                f"{i}. [{h['phase']}] {h['hypothesis']}\n"
                f"   総合スコア {h['total']}（成功{h['success']}×影響{h['impact']}÷コスト{h['cost']}）"
                f" {h.get('comment','')}\n"
                f"   最初の行動: {h.get('first_action','')}")
        best = res.get("best")
        if best:
            out.append(f"\n→ 推奨: [{best['phase']}] {best['hypothesis']}")
            out.append(f"   まず実行: {best.get('first_action','')}")
        return "\n".join(out)
