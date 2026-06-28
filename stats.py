# -*- coding: utf-8 -*-
#stats.py — 行動履歴を記録し、得意分野推定・最適モデル提案・履歴グラフ・性格プロファイルを出す
"""
全LLM呼び出し(role/provider/model/成否/ms)と、エージェントの行動(action種別/重複/ループ/
judge結果/assist頻度)を SQLite に記録。これを集計して以下を提供する:
  - model_specialties(): モデル×役割の成功率・速度 → 得意分野
  - recommend_routes(): 役割ごとの最適モデル順の提案
  - history(): 時系列の行動ログ（グラフ用）
  - personality(): assist頻度・ループ率などからエージェントの傾向プロファイル
"""
from __future__ import annotations

import os
import sqlite3
import time

_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stats.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, role TEXT, provider TEXT, model TEXT,
    ok INTEGER NOT NULL DEFAULT 1, ms INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT
);
"""


def _conn():
    c = sqlite3.connect(_DB)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def record_llm(role, provider, model, ok, ms=0):
    with _conn() as c:
        c.execute("INSERT INTO llm_calls (ts,role,provider,model,ok,ms) VALUES (?,?,?,?,?,?)",
                  (time.time(), role, provider, model, 1 if ok else 0, int(ms or 0)))


def record_event(kind, detail=""):
    with _conn() as c:
        c.execute("INSERT INTO events (ts,kind,detail) VALUES (?,?,?)",
                  (time.time(), kind, str(detail)[:200]))


def observe(event: str, payload: dict):
    """registry/agent_loop の観測フックから呼ぶ。LLM結果と行動を記録する。"""
    try:
        if event == "recv":
            record_llm(payload.get("role"), payload.get("provider"),
                       payload.get("model"), True, payload.get("ms", 0))
        elif event in ("limit", "error", "fail"):
            record_llm(payload.get("role"), payload.get("provider"),
                       payload.get("model"), False, 0)
        elif event == "flow":
            t = payload.get("type")
            if t in ("action", "duplicate", "loop_detected", "judge",
                     "objective_giveup", "action_error", "final_report"):
                d = ""
                if t == "action":
                    d = (payload.get("action") or {}).get("type", "")
                elif t == "judge":
                    d = "done" if payload.get("done") else "notdone"
                record_event(t, d)
    except Exception:
        pass


def model_specialties() -> list[dict]:
    """モデル×役割ごとの成功率・平均速度・回数（得意分野推定）。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT role, provider, model, COUNT(*) n, "
            "SUM(ok) oks, AVG(CASE WHEN ok=1 THEN ms END) avg_ms "
            "FROM llm_calls GROUP BY role, provider, model").fetchall()
    out = []
    for r in rows:
        n = r["n"]; oks = r["oks"] or 0
        out.append({
            "role": r["role"], "provider": r["provider"], "model": r["model"],
            "calls": n, "success_rate": round(oks / n, 3) if n else 0,
            "avg_ms": int(r["avg_ms"] or 0),
        })
    return out


def recommend_routes() -> dict:
    """役割ごとに、成功率→速度 の順で最適なモデルを提案する。"""
    spec = model_specialties()
    by_role: dict[str, list] = {}
    for s in spec:
        if s["calls"] < 1:
            continue
        by_role.setdefault(s["role"], []).append(s)
    rec = {}
    for role, items in by_role.items():
        # 成功率が高く、速い(ms小)順。回数も少し加味
        items.sort(key=lambda x: (-x["success_rate"], x["avg_ms"] or 1e9))
        rec[role] = [{"provider": i["provider"], "model": i["model"],
                      "success_rate": i["success_rate"], "avg_ms": i["avg_ms"],
                      "calls": i["calls"]} for i in items]
    return rec


def history(limit: int = 300) -> list[dict]:
    """行動イベントの時系列（グラフ用）。"""
    with _conn() as c:
        rows = c.execute("SELECT ts, kind, detail FROM events "
                         "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"ts": r["ts"], "kind": r["kind"], "detail": r["detail"]}
            for r in reversed(rows)]


def personality() -> dict:
    """行動傾向からエージェントの“性格”プロファイルを数値化(0-100)。"""
    with _conn() as c:
        ev = c.execute("SELECT kind, COUNT(*) n FROM events GROUP BY kind").fetchall()
        actions = c.execute("SELECT detail, COUNT(*) n FROM events "
                            "WHERE kind='action' GROUP BY detail").fetchall()
    cnt = {r["kind"]: r["n"] for r in ev}
    total_act = cnt.get("action", 0) or 1
    act_kinds = {r["detail"]: r["n"] for r in actions}
    # 各指標を割合→0-100に
    def pct(x, base): return round(min(100, 100 * x / base)) if base else 0
    judges = cnt.get("judge", 0) or 1
    prof = {
        "慎重さ": pct(act_kinds.get("assist", 0), total_act),        # assistを出すほど慎重
        "探索性": pct(act_kinds.get("web_search", 0), total_act),    # 検索を好む
        "実行力": pct(act_kinds.get("command", 0) + act_kinds.get("code", 0), total_act),
        "粘り強さ": 100 - pct(cnt.get("objective_giveup", 0), max(cnt.get("final_report", 1), 1)),
        "安定性": 100 - pct(cnt.get("loop_detected", 0) + cnt.get("action_error", 0), total_act),
    }
    return {"profile": prof,
            "action_breakdown": act_kinds,
            "totals": {"actions": cnt.get("action", 0),
                       "duplicates": cnt.get("duplicate", 0),
                       "loops": cnt.get("loop_detected", 0),
                       "giveups": cnt.get("objective_giveup", 0),
                       "errors": cnt.get("action_error", 0)}}


def clear():
    if os.path.exists(_DB):
        os.remove(_DB)
