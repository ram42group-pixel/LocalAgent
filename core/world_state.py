# -*- coding: utf-8 -*-
#core/world_state.py — エージェントの世界状態を永続管理する（Phase2）
"""
観測から得た事実・推測・仮説、試した経路・行き止まり・確定した発見を
一元管理する。仮説駆動ループの中核データ構造であり、
ハルシネーション防止（事実照合）の参照元でもある。

状態:
  services / versions   発見したサービスとバージョン（事実）
  facts                 構造化された事実（type/name/value/confidence）
  assumptions           未検証の推測
  hypotheses            検証可能な仮説（status付き）
  tested_paths          試した行動（重複探索の防止）
  confirmed_findings    確定した発見（フラグ・脆弱性実証等）
  dead_ends             行き止まり（再挑戦しない）
  active_rules          適用中のルール（参照のみ。実体はLTM）

SQLite永続化。1エンゲージメント=1DB（既定 world_state.db）。
"""
from __future__ import annotations
import os
import json
import sqlite3
import time

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "world_state.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, type TEXT, name TEXT, value TEXT,
    confidence REAL DEFAULT 1.0, source TEXT,
    UNIQUE(type, name, value)
);
CREATE TABLE IF NOT EXISTS assumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, statement TEXT UNIQUE, confidence REAL, basis TEXT
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, description TEXT UNIQUE, confidence REAL,
    evidence TEXT, next_steps TEXT, status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS tested_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, path TEXT UNIQUE, outcome TEXT
);
CREATE TABLE IF NOT EXISTS dead_ends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, path TEXT UNIQUE, reason TEXT
);
CREATE TABLE IF NOT EXISTS confirmed_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, finding TEXT UNIQUE, detail TEXT
);
CREATE TABLE IF NOT EXISTS executed_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, host TEXT
);
CREATE TABLE IF NOT EXISTS rejected_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, host TEXT
);
CREATE TABLE IF NOT EXISTS target_graph (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, target TEXT UNIQUE, status TEXT DEFAULT 'candidate',
    source TEXT, parent TEXT, evidence TEXT, confidence REAL DEFAULT 0.0,
    relation TEXT DEFAULT '', is_root INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS target_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, target TEXT, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);
"""


class WorldState:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        try:
            self.conn.execute("PRAGMA busy_timeout=10000")
            if db_path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate_target_graph()

    def _migrate_target_graph(self):
        """旧DB（Phase4.1のtarget_graph）に relation/is_root 列が無ければ追加する。"""
        try:
            cols = {r["name"] for r in self.conn.execute(
                "PRAGMA table_info(target_graph)").fetchall()}
            with self.conn:
                if "relation" not in cols:
                    self.conn.execute(
                        "ALTER TABLE target_graph ADD COLUMN relation TEXT DEFAULT ''")
                if "is_root" not in cols:
                    self.conn.execute(
                        "ALTER TABLE target_graph ADD COLUMN is_root INTEGER DEFAULT 0")
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---- 事実 ----
    def add_fact(self, type: str, name: str, value: str = "",
                 confidence: float = 1.0, source: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO facts (created_at,type,name,value,confidence,source) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), type, name, value, confidence, source))
            # 既存なら confidence は高い方を残す
            self.conn.execute(
                "UPDATE facts SET confidence=MAX(confidence,?) "
                "WHERE type=? AND name=? AND value=?",
                (confidence, type, name, value))

    def add_facts(self, facts) -> int:
        n = 0
        for f in facts:
            try:
                self.add_fact(f.type, f.name, f.value, f.confidence, f.source)
                n += 1
            except Exception:
                pass
        return n

    def facts(self, type: str = "") -> list[dict]:
        if type:
            rows = self.conn.execute(
                "SELECT * FROM facts WHERE type=? ORDER BY id", (type,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM facts ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def services(self) -> list[dict]:
        """サービス事実（name+version）を返す。ハルシネーション照合の主対象。"""
        return [{"name": r["name"], "version": r["value"],
                 "confidence": r["confidence"]}
                for r in self.conn.execute(
                    "SELECT name, value, confidence FROM facts WHERE type='service'"
                ).fetchall()]

    def known_service_names(self) -> set:
        return {r["name"].lower() for r in self.conn.execute(
            "SELECT name FROM facts WHERE type IN ('service','service_hint')"
        ).fetchall()}

    # ---- 推測 ----
    def add_assumption(self, statement: str, confidence: float = 0.3,
                       basis: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO assumptions (created_at,statement,confidence,basis) "
                "VALUES (?,?,?,?)", (time.time(), statement, confidence, basis))

    def assumptions(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM assumptions ORDER BY id").fetchall()]

    # ---- 仮説 ----
    def add_hypothesis(self, description: str, confidence: float = 0.0,
                       evidence=None, next_steps=None, status: str = "open") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO hypotheses "
                "(created_at,description,confidence,evidence,next_steps,status) "
                "VALUES (?,?,?,?,?,?)",
                (time.time(), description, confidence,
                 json.dumps(evidence or [], ensure_ascii=False),
                 json.dumps(next_steps or [], ensure_ascii=False), status))

    def set_hypothesis_status(self, description: str, status: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE hypotheses SET status=? WHERE description=?",
                (status, description))

    def open_hypotheses(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM hypotheses WHERE status IN ('open','testing') "
            "ORDER BY confidence DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["evidence"] = json.loads(d.get("evidence") or "[]")
                d["next_steps"] = json.loads(d.get("next_steps") or "[]")
            except Exception:
                d["evidence"] = []; d["next_steps"] = []
            out.append(d)
        return out

    # ---- 探索の足跡 ----
    def mark_tested(self, path: str, outcome: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO tested_paths (created_at,path,outcome) "
                "VALUES (?,?,?)", (time.time(), path, outcome))

    def is_tested(self, path: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM tested_paths WHERE path=?", (path,)).fetchone() is not None

    def mark_dead_end(self, path: str, reason: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO dead_ends (created_at,path,reason) "
                "VALUES (?,?,?)", (time.time(), path, reason))

    def dead_ends(self) -> list[str]:
        return [r["path"] for r in self.conn.execute(
            "SELECT path FROM dead_ends").fetchall()]

    def add_finding(self, finding: str, detail: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO confirmed_findings (created_at,finding,detail) "
                "VALUES (?,?,?)", (time.time(), finding, detail))

    # ---- Phase4: ターゲット整合性の記録 ----
    def add_executed_target(self, host: str) -> None:
        if not host:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO executed_targets (created_at, host) VALUES (?,?)",
                (time.time(), host))

    def add_rejected_target(self, host: str) -> None:
        if not host:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO rejected_targets (created_at, host) VALUES (?,?)",
                (time.time(), host))

    def executed_targets(self) -> list[str]:
        return [r["host"] for r in self.conn.execute(
            "SELECT host FROM executed_targets ORDER BY id").fetchall()]

    def rejected_targets(self) -> list[str]:
        return [r["host"] for r in self.conn.execute(
            "SELECT host FROM rejected_targets ORDER BY id").fetchall()]

    # ---- Phase4.1: 証拠付きターゲットグラフ ----
    def add_target_node(self, target: str, status: str, source: str = "",
                        parent: str = "", evidence: str = "",
                        confidence: float = 0.0, relation: str = "",
                        is_root: bool = False) -> None:
        """ターゲットをScope Graphに追加/更新する（provenance付き）。
        status: 'trusted' / 'candidate' / 'rejected'。
        relation: 親との関係（dns/redirects_to/links_to/certificate/api 等）。
        is_root: Root Target（ユーザー指定の起点）か。
        既存ノードは、より高い信頼度・より強いstatusで上書きする。"""
        if not target:
            return
        _rank = {"rejected": 0, "candidate": 1, "trusted": 2}
        with self.conn:
            row = self.conn.execute(
                "SELECT status, confidence FROM target_graph WHERE target=?",
                (target,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO target_graph "
                    "(created_at,target,status,source,parent,evidence,confidence,"
                    "relation,is_root) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), target, status, source, parent, evidence,
                     confidence, relation, 1 if is_root else 0))
            else:
                # 既存より強いstatus・高信頼なら更新。
                # is_root への昇格は status/confidence に関わらず常に反映する。
                if (_rank.get(status, 0) > _rank.get(row["status"], 0)
                        or confidence > (row["confidence"] or 0.0)):
                    self.conn.execute(
                        "UPDATE target_graph SET status=?, source=?, parent=?, "
                        "evidence=?, confidence=MAX(confidence,?), relation=?, "
                        "is_root=MAX(is_root,?) WHERE target=?",
                        (status, source, parent, evidence, confidence, relation,
                         1 if is_root else 0, target))
                elif is_root:
                    # ノードを Root に昇格（既存が同status/同confidenceでも）
                    self.conn.execute(
                        "UPDATE target_graph SET is_root=1 WHERE target=?",
                        (target,))
            self.conn.execute(
                "INSERT INTO target_events (created_at,target,event,detail) "
                "VALUES (?,?,?,?)",
                (time.time(), target, status, f"{source}: {evidence}"[:160]))

    def trusted_targets(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT target, source, parent, evidence, confidence "
            "FROM target_graph WHERE status='trusted' ORDER BY id").fetchall()]

    def candidate_targets(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT target, source, parent, evidence, confidence "
            "FROM target_graph WHERE status='candidate' ORDER BY id").fetchall()]

    def trusted_target_names(self) -> set:
        return {r["target"] for r in self.conn.execute(
            "SELECT target FROM target_graph WHERE status='trusted'").fetchall()}

    def promote_target(self, target: str, source: str = "",
                       evidence: str = "", confidence: float = 1.0) -> None:
        """candidate を trusted へ昇格（証拠が得られたとき）。"""
        self.add_target_node(target, "trusted", source=source,
                             evidence=evidence, confidence=confidence)

    def target_graph(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT target, status, source, parent, evidence, confidence, "
            "relation, is_root FROM target_graph ORDER BY id").fetchall()]

    def target_events(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT created_at, target, event, detail FROM target_events "
            "ORDER BY id").fetchall()]

    # ---- Phase4.2: Scope Graph / Evidence Chain ----
    def _node(self, target: str):
        if not target:
            return None
        r = self.conn.execute(
            "SELECT target, status, source, parent, evidence, confidence, "
            "relation, is_root FROM target_graph WHERE target=?",
            (target,)).fetchone()
        return dict(r) if r else None

    def evidence_chain(self, target: str) -> list[dict]:
        """target から Root まで parent リンクを辿り、証拠経路を返す。
        到達できない（Root に行き着かない/途中で途切れる）場合は空リスト。
        返り値は [target_node, ..., root_node] の順（葉→根）。"""
        chain = []
        seen = set()
        cur = (target or "").lower().strip()
        while cur and cur not in seen:
            seen.add(cur)
            node = self._node(cur)
            if node is None:
                return []          # グラフに無い → 経路なし
            chain.append(node)
            if node.get("is_root"):
                return chain        # Root に到達 → 有効な証拠経路
            parent = (node.get("parent") or "").lower().strip()
            if not parent:
                return []          # 親が無いのに Root でない → 経路断絶
            cur = parent
        return []                   # ループ等で Root に行き着かなかった

    def is_reachable(self, target: str) -> bool:
        """target が Root から Evidence Chain で到達可能か。
        各ノードが status!='rejected' であることも条件にする。"""
        chain = self.evidence_chain(target)
        if not chain:
            return False
        return all(n.get("status") != "rejected" for n in chain)

    def reachable_targets(self) -> set:
        """Root から証拠経路で到達可能な全ターゲット名の集合。"""
        out = set()
        for r in self.conn.execute("SELECT target FROM target_graph").fetchall():
            if self.is_reachable(r["target"]):
                out.add(r["target"])
        return out

    def chain_explanation(self, target: str) -> str:
        """『なぜここを探索しているか』を人間可読で返す（葉→根）。
        例: admin.xxx.com ←[HTML Link]← login.xxx.com ←[redirects_to]← xxx.com(root)"""
        chain = self.evidence_chain(target)
        if not chain:
            return f"{target}: Root への証拠経路なし（実行不可）"
        parts = []
        for i, n in enumerate(chain):
            label = n["target"] + ("(root)" if n.get("is_root") else "")
            parts.append(label)
            if i < len(chain) - 1:
                rel = n.get("relation") or n.get("source") or "?"
                parts.append(f" ←[{rel}]← ")
        return "".join(parts)

    # ---- 全体スナップショット（要件9の形式）----
    def snapshot(self) -> dict:
        svcs = self.services()
        return {
            "services": [s["name"] for s in svcs],
            "versions": [f"{s['name']} {s['version']}".strip() for s in svcs],
            "facts": self.facts(),
            "assumptions": self.assumptions(),
            "hypotheses": self.open_hypotheses(),
            "tested_paths": [r["path"] for r in self.conn.execute(
                "SELECT path FROM tested_paths ORDER BY id").fetchall()],
            "confirmed_findings": [dict(r) for r in self.conn.execute(
                "SELECT finding, detail FROM confirmed_findings").fetchall()],
            "dead_ends": self.dead_ends(),
        }

    def prompt_text(self) -> str:
        """プランナーへ注入する世界状態の要約。"""
        snap = self.snapshot()
        parts = []
        if snap["versions"]:
            parts.append("確定サービス: " + ", ".join(snap["versions"][:8]))
        if snap["hypotheses"]:
            parts.append("検証中の仮説: " + ", ".join(
                h["description"][:40] for h in snap["hypotheses"][:4]))
        if snap["dead_ends"]:
            parts.append("行き止まり(再挑戦不可): " + ", ".join(snap["dead_ends"][:5]))
        if not parts:
            return ""
        return "\n【世界状態（観測事実ベース）】" + " ／ ".join(parts)

    def clear(self) -> None:
        with self.conn:
            for t in ("facts", "assumptions", "hypotheses", "tested_paths",
                      "dead_ends", "confirmed_findings", "executed_targets",
                      "rejected_targets", "target_graph", "target_events"):
                self.conn.execute(f"DELETE FROM {t}")

    def get_meta(self, key: str, default: str = "") -> str:
        try:
            r = self.conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default
        except Exception:
            return default

    def set_meta(self, key: str, value: str) -> None:
        try:
            with self.conn:
                # INSERT OR REPLACE は全SQLite版で動く（UPSERTは3.24+必須のため避ける）
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)",
                    (key, value))
        except Exception:
            pass
