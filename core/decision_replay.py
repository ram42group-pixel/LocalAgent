# -*- coding: utf-8 -*-
"""Phase5: Replay API & Decision Visualization.

decision_trace.db を読み出し、Run を1ステップずつ再生・解析するための
読み取り専用API。WebUI / CLI から利用する。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Optional

from core.decision_provenance import DEFAULT_DB


def _conn(db_path: str = DEFAULT_DB):
    c = sqlite3.connect(db_path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=3000")
    except Exception:
        pass
    return c


def _loads(s: Any):
    try:
        return json.loads(s) if s else None
    except Exception:
        return s


def list_runs(limit: int = 50, db_path: str = DEFAULT_DB) -> list:
    """最近の run を新しい順に返す。"""
    try:
        c = _conn(db_path)
        rows = c.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_run(trace_id: str, db_path: str = DEFAULT_DB) -> dict:
    """1 run の全 decision を順番に返す（Replay用）。"""
    try:
        c = _conn(db_path)
        run = c.execute("SELECT * FROM runs WHERE trace_id=?",
                        (trace_id,)).fetchone()
        decs = c.execute(
            "SELECT * FROM decisions WHERE trace_id=? ORDER BY turn, created_at",
            (trace_id,)).fetchall()
        c.close()
        out = {"run": dict(run) if run else {}, "decisions": []}
        for d in decs:
            dd = dict(d)
            for k in ("observation_ids", "fact_ids", "evidence_ids",
                      "hypothesis_ids", "candidate_hypotheses", "rule_ids"):
                dd[k] = _loads(dd.get(k))
            dd["critic"] = _loads(dd.get("critic"))
            out["decisions"].append(dd)
        return out
    except Exception:
        return {"run": {}, "decisions": []}


def get_decision(decision_id: str, db_path: str = DEFAULT_DB) -> dict:
    """1 decision の詳細＋その間に起きた生イベントを返す。"""
    try:
        c = _conn(db_path)
        d = c.execute("SELECT * FROM decisions WHERE decision_id=?",
                      (decision_id,)).fetchone()
        if not d:
            c.close()
            return {}
        dd = dict(d)
        for k in ("observation_ids", "fact_ids", "evidence_ids",
                  "hypothesis_ids", "candidate_hypotheses", "rule_ids"):
            dd[k] = _loads(dd.get(k))
        dd["critic"] = _loads(dd.get("critic"))
        events = c.execute(
            "SELECT etype,payload,seq,created_at FROM events "
            "WHERE decision_id=? ORDER BY seq", (decision_id,)).fetchall()
        dd["events"] = [{"etype": e["etype"], "seq": e["seq"],
                         "payload": _loads(e["payload"]),
                         "created_at": e["created_at"]} for e in events]
        c.close()
        return dd
    except Exception:
        return {}


def provenance_chain(decision_id: str, db_path: str = DEFAULT_DB) -> dict:
    """1つのCommandの由来チェーンを返す（順・逆両方向）。
    Observation→Fact→Evidence→Hypothesis→Decision→Command を辿る。"""
    d = get_decision(decision_id, db_path)
    if not d:
        return {}
    forward = []
    for label, key in (("Observation", "observation_ids"),
                       ("Fact", "fact_ids"),
                       ("Evidence", "evidence_ids"),
                       ("Hypothesis", "hypothesis_ids")):
        ids = d.get(key) or []
        if ids:
            forward.append({"stage": label, "ids": ids})
    forward.append({"stage": "Decision", "id": decision_id,
                    "strategy": d.get("strategy", ""),
                    "chosen_hypothesis": d.get("chosen_hypothesis", "")})
    forward.append({"stage": "Command", "value": d.get("command", "")})
    forward.append({"stage": "Result", "value": (d.get("result") or "")[:200]})
    return {"decision_id": decision_id, "forward": forward,
            "reverse": list(reversed(forward))}


def decision_graph(trace_id: str, db_path: str = DEFAULT_DB) -> dict:
    """Decision Graph（可視化用のnodes/edges）を返す。
    各 decision をノード、turn の前後関係＋仮説/戦略の継続をエッジにする。"""
    run = get_run(trace_id, db_path)
    nodes, edges = [], []
    prev_id = None
    for d in run["decisions"]:
        did = d["decision_id"]
        status = ("blocked" if d.get("blocked") else
                  "fail" if d.get("hallucination") else
                  "ok")
        nodes.append({
            "id": did, "turn": d.get("turn"),
            "command": (d.get("command") or "")[:60],
            "strategy": d.get("strategy", ""),
            "hypothesis": (d.get("chosen_hypothesis") or "")[:60],
            "status": status,
            "result": (d.get("result") or "")[:80],
        })
        if prev_id:
            edges.append({"from": prev_id, "to": did, "kind": "next"})
        prev_id = did
    return {"trace_id": trace_id, "nodes": nodes, "edges": edges}


def tool_metrics(db_path: str = DEFAULT_DB) -> list:
    """Tool別のメトリクス（成功率・平均時間等）を返す。"""
    try:
        c = _conn(db_path)
        rows = c.execute(
            "SELECT * FROM tool_metrics ORDER BY uses DESC").fetchall()
        c.close()
        out = []
        for r in rows:
            uses = r["uses"] or 1
            out.append({
                "tool": r["tool"], "uses": r["uses"],
                "success_rate": round(r["successes"] / uses, 3),
                "failure_rate": round(r["failures"] / uses, 3),
                "error_rate": round(r["errors"] / uses, 3),
                "avg_ms": round((r["total_ms"] or 0) / uses, 1),
            })
        return out
    except Exception:
        return []


def run_metrics(trace_id: str, db_path: str = DEFAULT_DB) -> dict:
    """1 run の品質メトリクス（Reflection強化用）を集計して返す。"""
    try:
        c = _conn(db_path)
        decs = c.execute("SELECT * FROM decisions WHERE trace_id=?",
                         (trace_id,)).fetchall()
        halls = c.execute("SELECT COUNT(*) n FROM hallucinations WHERE trace_id=?",
                          (trace_id,)).fetchone()
        pms = c.execute("SELECT est_tokens FROM prompt_metrics WHERE trace_id=? "
                        "ORDER BY id", (trace_id,)).fetchall()
        c.close()
        n = len(decs) or 1
        blocked = sum(1 for d in decs if d["blocked"])
        strategies = [d["strategy"] for d in decs if d["strategy"]]
        switches = sum(1 for i in range(1, len(strategies))
                       if strategies[i] != strategies[i - 1])
        ok = sum(1 for d in decs
                 if not d["blocked"] and not _looks_fail(d["result"]))
        return {
            "decisions": len(decs),
            "decision_success_rate": round(ok / n, 3),
            "hallucination_rate": round((halls["n"] or 0) / n, 3),
            "scope_violations": blocked,
            "strategy_switches": switches,
            "rule_usage": sum(1 for d in decs if d["rule_ids"] not in ("", "[]", None)),
            "prompt_token_trend": [p["est_tokens"] for p in pms],
        }
    except Exception:
        return {}


def _looks_fail(result: Any) -> bool:
    s = str(result or "").lower()
    return ("error" in s or "失敗" in s or "no targets" in s
            or "not found" in s or "エラー" in s)
