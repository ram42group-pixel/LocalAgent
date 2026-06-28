# -*- coding: utf-8 -*-
#tools/registry.py — ツールの登録と呼び出しを集約
"""
ツール追加 = _TOOLS に1行足すだけ。
  run_tool(name, args) で実行。list_specs() でLLMへ提示する仕様一覧を得る。
"""
from __future__ import annotations

from tools.builtins import (CalculatorTool, HttpGetTool, FileSummarizeTool,
                            GitTool, AppDetectTool)
from tools.security import CveLookupTool, MetasploitTool, SqlmapTool, McpTool
from tools.browser import BrowserTool, VisionTool, WebScanTool, WebInspectTool
from tools.expert_tool import ExpertTool, ExpertsParallelTool
from tools.recon_tool import RecordFindingTool, AttackStateTool
from tools.exploit_tool import ExploitRunTool, PrivescTool, LateralTool
from tools.strategy_tool import StrategizeTool
from tools.report_tool import ReportTool

# 名前 → ツールクラス（新ツールはここに登録）
_TOOLS = {
    "calculator": CalculatorTool,
    "http_get": HttpGetTool,
    "file_summarize": FileSummarizeTool,
    "git": GitTool,
    "app_detect": AppDetectTool,
    "cve_lookup": CveLookupTool,
    "metasploit": MetasploitTool,
    "browser": BrowserTool,
    "vision": VisionTool,
    "web_scan": WebScanTool,
    "web_inspect": WebInspectTool,
    "sqlmap": SqlmapTool,
    "mcp": McpTool,
    "expert": ExpertTool,
    "experts_parallel": ExpertsParallelTool,
    "record": RecordFindingTool,
    "attack_state": AttackStateTool,
    "exploit_run": ExploitRunTool,
    "privesc": PrivescTool,
    "lateral": LateralTool,
    "strategize": StrategizeTool,
    "report": ReportTool,
}

_cache: dict[str, object] = {}

# ツールの有効/無効状態（UI/別ページから切替・tools_state.jsonに永続化）
import json as _json
import os as _os
_STATE_FILE = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                            "tools_state.json")


def _load_state() -> dict:
    try:
        with open(_STATE_FILE, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            _json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_enabled = _load_state()       # {tool_name: bool}。未登録のツールは既定で有効。


def is_enabled(name: str) -> bool:
    return _enabled.get(name, True)


def set_enabled(name: str, enabled: bool) -> None:
    if name in _TOOLS:
        _enabled[name] = bool(enabled)
        _save_state(_enabled)


def enabled_tools() -> list[str]:
    return [n for n in _TOOLS if is_enabled(n)]


def _get(name: str):
    if name not in _TOOLS:
        return None
    if name not in _cache:
        _cache[name] = _TOOLS[name]()
    return _cache[name]


def tool_names() -> list[str]:
    return list(_TOOLS)


def list_specs(only_enabled: bool = True) -> list[dict]:
    """LLMへ提示するツール仕様一覧（既定で有効なツールのみ）。"""
    names = enabled_tools() if only_enabled else list(_TOOLS)
    return [_get(n).spec() for n in names]


def all_status() -> list[dict]:
    """UI用：全ツールの仕様＋有効状態を返す（無効も含む）。"""
    out = []
    for n in _TOOLS:
        spec = _get(n).spec()
        spec["enabled"] = is_enabled(n)
        out.append(spec)
    return out


def specs_text() -> str:
    """プロンプト注入用の短い説明テキスト（有効なツールのみ）。"""
    lines = []
    for s in list_specs(only_enabled=True):
        a = ", ".join(f"{k}({v})" for k, v in s["args"].items())
        lines.append(f"- {s['name']}: {s['description']} / 引数: {a}")
    return "\n".join(lines)


def run_tool(name: str, args: dict) -> str:
    if name not in _TOOLS:
        return f"エラー: 未登録のツール: {name}（使えるのは {', '.join(_TOOLS)}）"
    if not is_enabled(name):
        return f"エラー: ツール '{name}' は無効化されています（設定画面で有効化できます）"
    t = _get(name)
    try:
        return t.run(args or {})
    except Exception as ex:
        return f"エラー: ツール実行失敗: {ex}"
