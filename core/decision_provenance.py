# -*- coding: utf-8 -*-
"""Phase5: Decision Provenance & Replay System.

すべての意思決定について「なぜその判断に至ったか」を完全な因果チェーンとして
記録・再生する基盤。Observation→Fact→Evidence→Hypothesis→Decision→Strategy→
Command→Result→Critic→Lesson→Rule を1つの decision_id で結ぶ。

設計方針:
- 既存機能は一切変更しない。agent_loop の _emit を観測して受動的に組み立てる。
- 詳細トレースは decision_trace.db（SQLite）に長期保存。
- 1 run = 1 trace_id, 1手 = 1 decision_id。
- 失敗しても agent 本体を止めない（全API例外安全）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Any, Optional

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "decision_trace.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    trace_id TEXT PRIMARY KEY,
    request TEXT, goal TEXT, mode TEXT,
    primary_target TEXT, started_at REAL, ended_at REAL,
    status TEXT DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    trace_id TEXT, turn INTEGER, objective TEXT,
    observation_ids TEXT, fact_ids TEXT, evidence_ids TEXT,
    hypothesis_ids TEXT, candidate_hypotheses TEXT, chosen_hypothesis TEXT,
    exploration_score REAL, strategy TEXT, rule_ids TEXT,
    planner_reason TEXT, prompt_summary TEXT,
    command TEXT, result TEXT, critic TEXT, lesson TEXT,
    blocked INTEGER DEFAULT 0, hallucination TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS prompt_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT, decision_id TEXT, role TEXT,
    prompt_chars INTEGER, est_tokens INTEGER, ctx_usage REAL,
    history_size INTEGER, world_size INTEGER, rule_count INTEGER,
    memory_count INTEGER, fact_count INTEGER, observation_count INTEGER,
    completion_tokens INTEGER, response_ms INTEGER,
    empty_count INTEGER, retry_count INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS tool_metrics (
    tool TEXT PRIMARY KEY,
    uses INTEGER DEFAULT 0, successes INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
    total_ms REAL DEFAULT 0, total_findings INTEGER DEFAULT 0,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS hallucinations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT, decision_id TEXT, kind TEXT, detail TEXT,
    cause_decision TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT, decision_id TEXT, seq INTEGER,
    etype TEXT, payload TEXT, created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_dec_trace ON decisions(trace_id);
CREATE INDEX IF NOT EXISTS idx_ev_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_pm_trace ON prompt_metrics(trace_id);
"""


def _est_tokens(text: str) -> int:
    """大まかなトークン推定（英数は ~4字/token, 日本語は ~1.5字/token の中間）。"""
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    # ASCII比率で粗く按分
    ascii_n = sum(1 for c in text if ord(c) < 128)
    non = len(text) - ascii_n
    return int(ascii_n / 4 + non / 1.8) + 1


class DecisionProvenance:
    """1 run 分の意思決定トレースを記録するレコーダ。

    使い方（agent_loop 側）:
        prov = DecisionProvenance(); prov.start_run(request, goal, mode, target)
        prov.observe(event_dict)        # _emit のたびに渡す（受動収集）
        prov.finish_run(status)
    observe() がイベント種別を見て decision を区切り、DBへ確定保存する。
    """

    # context window 既定値（モデル依存だが警告閾値の基準）
    CTX_WINDOW = 32768
    PROMPT_WARN_TOKENS = 24000

    def __init__(self, db_path: str = DEFAULT_DB, emit=None):
        self.db_path = db_path
        self._emit = emit            # 警告イベントを上流へ流すための任意フック
        self.trace_id = ""
        self.turn = 0
        self.seq = 0
        self._cur: dict = {}         # 組み立て中の decision
        self._last_decision_id = ""
        self._empty_count = 0
        self._retry_count = 0
        self._llm_t0 = 0.0
        self._tool_t0 = 0.0
        self._tool_name = ""
        self._open = False
        self._in_observe = False
        self._objective = ""
        try:
            self.conn = sqlite3.connect(db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            # 並行read(Replay UI)とwrite(記録)が衝突しても待てるように
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=3000")
                self.conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
            self.conn.executescript(_SCHEMA)
            self._open = True
        except Exception:
            self.conn = None

    # ---- run ライフサイクル ---- #
    def start_run(self, request: str, goal: str = "", mode: str = "",
                  primary_target: str = "") -> str:
        self.trace_id = uuid.uuid4().hex[:16]
        self.turn = 0
        self.seq = 0
        self._cur = {}
        if not self.conn:
            return self.trace_id
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO runs "
                    "(trace_id,request,goal,mode,primary_target,started_at,status)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (self.trace_id, request[:2000], goal[:2000], mode,
                     primary_target, time.time(), "running"))
        except Exception:
            pass
        return self.trace_id

    def finish_run(self, status: str = "done") -> None:
        self._flush_decision()       # 途中の decision があれば確定
        if not self.conn:
            return
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE runs SET ended_at=?, status=? WHERE trace_id=?",
                    (time.time(), status, self.trace_id))
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self._open = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- 受動観測（_emit の各イベントをここへ）---- #
    def observe(self, e: dict) -> None:
        if not isinstance(e, dict):
            return
        # 再入防止: observe 内で発火した警告イベントが _emit 経由で再び observe を
        # 呼ぶと _cur 状態が壊れる。処理中は二重実行しない。
        if getattr(self, "_in_observe", False):
            return
        self._in_observe = True
        try:
            t = e.get("type", "")
            self.seq += 1
            try:
                self._raw_event(t, e)
            except Exception:
                pass
            try:
                self._route(t, e)
            except Exception:
                pass
        finally:
            self._in_observe = False

    def _route(self, t: str, e: dict) -> None:
        # --- 現在の objective を保持（各decisionに付与）---
        if t == "objective_start":
            self._objective = str(e.get("objective", ""))[:500]
            return
        # --- goal 確定時に runs テーブルへ反映 ---
        if t == "goal_done":
            try:
                if self.conn:
                    with self.conn:
                        self.conn.execute(
                            "UPDATE runs SET goal=? WHERE trace_id=?",
                            (str(e.get("goal", ""))[:2000], self.trace_id))
            except Exception:
                pass
            return
        # --- decision 区切り: plan_next_action が新しい1手の開始 ---
        if t == "fn" and e.get("fn") == "plan_next_action":
            self._flush_decision()           # 前の手を確定
            self._begin_decision()
        # --- LLM 計測 ---
        elif t == "llm_request":
            self._llm_t0 = time.time()
            self._capture_prompt(e)
        elif t == "llm_response":
            self._finish_prompt(e)
        elif t == "empty_response":
            self._empty_count += 1
            self._cur["empty"] = self._cur.get("empty", 0) + 1
        elif t == "parse_error":
            self._retry_count += 1
        # --- 仮説/戦略/ルール/探索 ---
        elif t == "hypothesis_chosen":
            self._cur["chosen_hypothesis"] = e.get("hypothesis", "")
            self._cur["candidate_hypotheses"] = e.get("candidates", [])
            self._cur["exploration_score"] = e.get("score", 0.0)
        elif t == "strategy" or t == "strategy_switch":
            self._cur["strategy"] = e.get("strategy", e.get("to", ""))
        elif t == "rules_applied":
            self._cur["rule_ids"] = e.get("ids", [])
        elif t == "exploration_metrics":
            if "exploration_score" not in self._cur:
                self._cur["exploration_score"] = e.get("score", 0.0)
        # --- planner の決定（action）---
        elif t == "action":
            self.turn = e.get("turn", self.turn)
            self._cur["turn"] = self.turn
            act = e.get("action", {}) or {}
            self._cur["command"] = self._action_text(act)
            self._cur["planner_reason"] = act.get("reason", "")
            self._cur["action_type"] = act.get("type", "")
            if act.get("type") == "tool":
                self._tool_name = act.get("name", "")
            else:
                self._tool_name = (self._action_text(act).split() or [""])[0]
            self._tool_t0 = time.time()
        # --- 実行結果 ---
        elif t == "exec_result":
            self._cur["result"] = str(e.get("result", ""))[:4000]
            self._record_tool_metric(success=not _is_fail(e.get("result", "")))
        elif t == "action_error":
            self._cur["result"] = "ERROR: " + str(e.get("error", ""))[:2000]
            self._record_tool_metric(success=False, error=True)
        # --- 観測分析（fact/hypothesis 件数）---
        elif t == "observation_analysis":
            self._cur["fact_count"] = e.get("facts", 0)
            self._cur["hyp_count"] = e.get("hypotheses", 0)
        # --- 批評/教訓 ---
        elif t == "critique":
            self._cur["critic"] = json.dumps(
                {"success": e.get("success"), "cause": e.get("cause", ""),
                 "improvement": e.get("improvement", "")}, ensure_ascii=False)
            self._cur["lesson"] = e.get("improvement", "")
        # --- ブロック/幻覚 ---
        elif t == "target_mismatch_blocked":
            self._cur["blocked"] = 1
            self._record_hallucination(
                "scope_target", e.get("offending", ""), self._cur.get("id", ""))
        elif t == "ip_guess_blocked":
            self._cur["blocked"] = 1
            self._record_hallucination(
                "ip_guess", e.get("reason", ""), self._cur.get("id", ""))
        elif t == "hallucination_blocked":
            self._cur["blocked"] = 1
            self._record_hallucination(
                "fact_mismatch", e.get("reason", ""), self._cur.get("id", ""))

    # ---- decision 組み立て ---- #
    def _begin_decision(self) -> None:
        self._cur = {
            "id": uuid.uuid4().hex[:16],
            "trace_id": self.trace_id,
            "turn": self.turn,
            "objective": getattr(self, "_objective", ""),
            "created_at": time.time(),
        }
        self._empty_count = 0
        self._retry_count = 0

    def _flush_decision(self) -> None:
        c = self._cur
        if not c or not c.get("id"):
            return
        # 空のdecision（commandもblockも無い）はスキップ
        if not c.get("command") and not c.get("blocked"):
            self._cur = {}
            return
        self._last_decision_id = c["id"]
        if not self.conn:
            self._cur = {}
            return
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO decisions ("
                    "decision_id,trace_id,turn,objective,observation_ids,fact_ids,"
                    "evidence_ids,hypothesis_ids,candidate_hypotheses,"
                    "chosen_hypothesis,exploration_score,strategy,rule_ids,"
                    "planner_reason,prompt_summary,command,result,critic,lesson,"
                    "blocked,hallucination,created_at) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["id"], c["trace_id"], c.get("turn", 0),
                     c.get("objective", ""),
                     _js(c.get("observation_ids", [])),
                     _js(c.get("fact_ids", [])),
                     _js(c.get("evidence_ids", [])),
                     _js(c.get("hypothesis_ids", [])),
                     _js(c.get("candidate_hypotheses", [])),
                     c.get("chosen_hypothesis", ""),
                     float(c.get("exploration_score", 0.0) or 0.0),
                     c.get("strategy", ""),
                     _js(c.get("rule_ids", [])),
                     c.get("planner_reason", ""),
                     c.get("prompt_summary", ""),
                     c.get("command", ""), c.get("result", ""),
                     c.get("critic", ""), c.get("lesson", ""),
                     int(c.get("blocked", 0)),
                     c.get("hallucination", ""),
                     c.get("created_at", time.time())))
        except Exception:
            pass
        self._cur = {}

    # ---- Prompt Analyzer ---- #
    def _capture_prompt(self, e: dict) -> None:
        try:
            msgs = e.get("messages", []) or []
            parts = []
            for m in msgs:
                if isinstance(m, dict):
                    c = m.get("content", "")
                    parts.append(c if isinstance(c, str) else str(c))
            text = "\n".join(parts)
            self._cur.setdefault("prompt_role", e.get("role", ""))
            self._cur["_prompt_text"] = text
            self._cur["_prompt_chars"] = len(text)
        except Exception:
            pass

    def _finish_prompt(self, e: dict) -> None:
        text = self._cur.get("_prompt_text", "")
        chars = self._cur.get("_prompt_chars", 0)
        est = _est_tokens(text)
        comp = _est_tokens(str(e.get("content", "")))
        ms = int((time.time() - self._llm_t0) * 1000) if self._llm_t0 else 0
        ctx_usage = round(est / self.CTX_WINDOW, 3)
        # prompt_summary を decision に保持（先頭360字）
        if "prompt_summary" not in self._cur:
            self._cur["prompt_summary"] = text[:360]
        if not self.conn:
            return
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO prompt_metrics (trace_id,decision_id,role,"
                    "prompt_chars,est_tokens,ctx_usage,history_size,world_size,"
                    "rule_count,memory_count,fact_count,observation_count,"
                    "completion_tokens,response_ms,empty_count,retry_count,"
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (self.trace_id, self._cur.get("id", ""),
                     self._cur.get("prompt_role", e.get("role", "")),
                     chars, est, ctx_usage, 0, 0,
                     len(self._cur.get("rule_ids", []) or []), 0,
                     self._cur.get("fact_count", 0), 0,
                     comp, ms, self._empty_count, self._retry_count,
                     time.time()))
        except Exception:
            pass
        # Prompt肥大化の警告
        if est >= self.PROMPT_WARN_TOKENS and self._emit:
            try:
                self._emit(type="prompt_warning", role=self._cur.get("prompt_role", ""),
                           est_tokens=est, ctx_usage=ctx_usage,
                           message=(f"プロンプトが大きすぎます（推定{est}token, "
                                    f"使用率{int(ctx_usage*100)}%）。"
                                    "文脈圧縮を検討してください。"))
            except Exception:
                pass
        self._cur.pop("_prompt_text", None)

    # ---- Tool Metrics ---- #
    def _record_tool_metric(self, success: bool, error: bool = False) -> None:
        tool = self._tool_name
        if not tool or not self.conn:
            return
        ms = (time.time() - self._tool_t0) * 1000 if self._tool_t0 else 0
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO tool_metrics (tool,uses,successes,failures,"
                    "errors,total_ms,total_findings,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(tool) DO UPDATE SET "
                    "uses=uses+1, successes=successes+?, failures=failures+?, "
                    "errors=errors+?, total_ms=total_ms+?, updated_at=?",
                    (tool, 1, 1 if success else 0, 0 if success else 1,
                     1 if error else 0, ms, 0, time.time(),
                     1 if success else 0, 0 if success else 1,
                     1 if error else 0, ms, time.time()))
        except Exception:
            # UPSERT 非対応環境向けフォールバック
            try:
                with self.conn:
                    row = self.conn.execute(
                        "SELECT * FROM tool_metrics WHERE tool=?", (tool,)).fetchone()
                    if row:
                        self.conn.execute(
                            "UPDATE tool_metrics SET uses=?,successes=?,failures=?,"
                            "errors=?,total_ms=?,updated_at=? WHERE tool=?",
                            (row["uses"]+1, row["successes"]+(1 if success else 0),
                             row["failures"]+(0 if success else 1),
                             row["errors"]+(1 if error else 0),
                             row["total_ms"]+ms, time.time(), tool))
                    else:
                        self.conn.execute(
                            "INSERT INTO tool_metrics (tool,uses,successes,failures,"
                            "errors,total_ms,total_findings,updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (tool, 1, 1 if success else 0, 0 if success else 1,
                             1 if error else 0, ms, 0, time.time()))
            except Exception:
                pass
        self._tool_t0 = 0.0

    # ---- Hallucination Metrics ---- #
    def _record_hallucination(self, kind: str, detail: str,
                              cause_decision: str = "") -> None:
        self._cur["hallucination"] = f"{kind}:{detail}"[:300]
        if not self.conn:
            return
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO hallucinations (trace_id,decision_id,kind,detail,"
                    "cause_decision,created_at) VALUES (?,?,?,?,?,?)",
                    (self.trace_id, self._cur.get("id", ""), kind,
                     str(detail)[:500], cause_decision, time.time()))
        except Exception:
            pass

    # ---- 生イベントログ（詳細トレース）---- #
    def _raw_event(self, t: str, e: dict) -> None:
        if not self.conn:
            return
        # ナレーター/解説など解析に不要な高頻度イベントは詳細ログから除外
        if t in ("", "fn") and not e.get("fn"):
            return
        try:
            payload = _js({k: v for k, v in e.items() if k != "type"})
            with self.conn:
                self.conn.execute(
                    "INSERT INTO events (trace_id,decision_id,seq,etype,payload,"
                    "created_at) VALUES (?,?,?,?,?,?)",
                    (self.trace_id, self._cur.get("id", ""), self.seq, t,
                     payload[:8000], time.time()))
        except Exception:
            pass

    # ---- ヘルパ ---- #
    @staticmethod
    def _action_text(act: dict) -> str:
        if not isinstance(act, dict):
            return str(act)
        for k in ("command", "url", "query", "name", "message"):
            v = act.get(k)
            if v:
                return str(v)
        return json.dumps(act, ensure_ascii=False)[:200]


def _js(v: Any) -> str:
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return "[]"


def _is_fail(result: Any) -> bool:
    s = str(result or "").lower()
    return ("error" in s or "失敗" in s or "no targets" in s
            or "0 hosts up" in s or "not found" in s or "エラー" in s)
