# -*- coding: utf-8 -*-
#mcp_server.py — LocalAgent を MCP サーバとして公開する（依存ゼロ・stdio JSON-RPC）
"""
他のMCPクライアント（Claude Desktop等）から LocalAgent の機能を呼べるようにする。
標準ライブラリのみ。stdin/stdout で JSON-RPC を喋る。

公開するツール:
  - run_agent      : 自然言語の指示でエージェントを実行（自律）
  - list_tools     : LocalAgent内蔵ツールの一覧
  - call_tool      : 内蔵ツールを直接1つ実行（cve_lookup, browser, vision 等）
  - search_memory  : 長期記憶をベクトル検索

クライアント設定例（Claude Desktop の mcp_servers.json 等）:
  {"localagent": {"command": "python", "args": ["/path/to/mcp_server.py"]}}
"""
from __future__ import annotations

import json
import sys

PROTOCOL = "2024-11-05"


# ---- 公開ツールの定義（MCP の inputSchema 形式）----
def _tool_defs() -> list[dict]:
    return [
        {"name": "run_agent",
         "description": "自然言語の指示でLocalAgentエージェントを自律実行し、最終結果を返す",
         "inputSchema": {"type": "object",
                         "properties": {"request": {"type": "string",
                                                    "description": "やってほしいこと"}},
                         "required": ["request"]}},
        {"name": "list_tools",
         "description": "LocalAgentが内蔵するツールの一覧を返す",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "call_tool",
         "description": "LocalAgentの内蔵ツールを1つ直接実行する（cve_lookup/browser/vision/calculator等）",
         "inputSchema": {"type": "object",
                         "properties": {"name": {"type": "string"},
                                        "args": {"type": "object"}},
                         "required": ["name"]}},
        {"name": "search_memory",
         "description": "LocalAgentの長期記憶を意味検索する",
         "inputSchema": {"type": "object",
                         "properties": {"query": {"type": "string"}},
                         "required": ["query"]}},
    ]


# ---- 各ツールの実処理 ----
def _call(name: str, args: dict) -> str:
    if name == "run_agent":
        return _run_agent(args.get("request", ""))
    if name == "list_tools":
        import tools.registry as tr
        specs = tr.list_specs()
        return json.dumps(specs, ensure_ascii=False, indent=2)
    if name == "call_tool":
        import tools.registry as tr
        return tr.run_tool(args.get("name", ""), args.get("args", {}) or {})
    if name == "search_memory":
        from memory import LongTermMemory
        with LongTermMemory() as ltm:
            hits = ltm.semantic_search(args.get("query", ""), limit=5)
        return json.dumps(hits, ensure_ascii=False, indent=2)
    return f"未知のツール: {name}"


def _run_agent(request: str) -> str:
    """エージェントを自律実行し、収集した最終レポートを文字列で返す。"""
    if not request:
        return "エラー: request が空です"
    import agent_loop
    import executor
    collected = {"report": None, "lines": []}

    def emit(e):
        t = e.get("type")
        if t == "final_report":
            collected["report"] = e
        elif t in ("objective_start", "exec_result"):
            collected["lines"].append(str(e.get("objective") or e.get("result"))[:120])

    agent_loop.run_agent(request, emit=emit, approver=executor.auto_yes,
                         dry_run=False)
    rep = collected["report"]
    if rep:
        out = [f"ゴール: {rep.get('goal')}",
               f"達成: {rep.get('done')}/{rep.get('total')}"]
        for r in rep.get("results", []):
            mark = "✓" if r.get("success") else "✗"
            out.append(f"  {mark} {r.get('objective')}: {r.get('summary')}")
        return "\n".join(out)
    return "完了（レポートなし）\n" + "\n".join(collected["lines"][-10:])


# ---- JSON-RPC ループ ----
def _handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params", {}) or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "LocalAgent", "version": "1.0"}}}
    if method == "notifications/initialized":
        return None                      # 通知には応答しない
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tool_defs()}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        try:
            text = _call(name, args)
        except Exception as ex:
            text = f"エラー: {ex}"
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    # 未知のメソッド
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"未対応: {method}"}}
    return None


def serve() -> None:
    """stdin から1行1JSONを読み、stdout に応答を書く。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        resp = _handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve()
