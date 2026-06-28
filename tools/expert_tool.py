# -*- coding: utf-8 -*-
#tools/expert_tool.py — 専門家に依頼するツール（単一・並列）
"""
オーケストレーター（エージェント本体）が、各ツールの専門家LLMに自然言語で依頼する。
- expert : 1人の専門家に依頼（引数決定→実行→専門家視点の解釈）
- experts_parallel : 複数の専門家に並列依頼（クラウドは並列、ollamaは逐次）
"""
from __future__ import annotations

import json

from tools.base import Tool


class ExpertTool(Tool):
    name = "expert"
    description = ("ツールの専門家LLMに自然言語で依頼する。専門家が引数を決め・実行し・"
                   "結果を専門家視点で解釈して返す。tool に依頼先（cve_lookup/sqlmap等）を指定")
    args = {"tool": "依頼する専門家のツール名（cve_lookup/sqlmap/web_scan/web_inspect/metasploit/browser/vision）",
            "request": "自然言語の依頼（例: target.comのSQLiを調べて）"}

    def run(self, args: dict) -> str:
        import experts
        tool = str(args.get("tool", "")).strip()
        request = str(args.get("request", "")).strip()
        if not tool or not request:
            return "エラー: tool と request が必要です"
        if not experts.get_expert(tool):
            return f"エラー: '{tool}' の専門家が未定義です（experts.jsonで設定可）"
        r = experts.ask_expert(tool, request)
        out = [f"== {tool} 専門家（{r.get('provider') or '直接実行'}）==",
               f"引数: {json.dumps(r['args'], ensure_ascii=False)}",
               f"\n[実行結果]\n{r['raw'][:1500]}"]
        if r.get("interpretation"):
            out.append(f"\n[専門家の見解]\n{r['interpretation']}")
        return "\n".join(out)


class ExpertsParallelTool(Tool):
    name = "experts_parallel"
    description = ("複数のツール専門家に並列で依頼する（クラウドLLMは同時実行）。"
                   "複数観点の調査を一度に進めたい時に使う")
    args = {"tasks": ('依頼の配列。例: '
                      '[{"tool":"cve_lookup","request":"Apache2.4.49のCVE"},'
                      '{"tool":"web_scan","request":"http://tの攻撃面"}]')}

    def run(self, args: dict) -> str:
        import experts
        tasks = args.get("tasks", [])
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except Exception:
                return "エラー: tasks はJSON配列で指定してください"
        tasks = [t for t in tasks if isinstance(t, dict) and t.get("tool") and t.get("request")]
        if not tasks:
            return "エラー: 有効な tasks がありません"
        results = experts.run_parallel(tasks)
        out = [f"== {len(results)}人の専門家が並列実行 =="]
        for r in results:
            out.append(f"\n【{r['tool']}専門家（{r.get('provider') or '直接'}）】")
            out.append(f"引数: {json.dumps(r['args'], ensure_ascii=False)}")
            view = r.get("interpretation") or r.get("raw", "")
            out.append(view[:800])
        return "\n".join(out)
