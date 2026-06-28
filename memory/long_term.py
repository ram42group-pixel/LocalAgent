# -*- coding: utf-8 -*-
#long_term.py — 長期記憶（SQLite・タグ索引）
"""
goal.txt の tags を索引にして、objective 完了ごとの要約を保存・検索する。
標準ライブラリ(sqlite3)のみ。DBファイルは memory/agent_memory.db に作られる。

  ltm = LongTermMemory()
  ltm.save(goal="...", objective="...", summary="...", tags=["log", "python"])
  ltm.search_by_tags(["log"])        # 一致タグ数の多い順 → 新しい順
  ltm.as_context_lines(["log"])      # build_context の related_memories にそのまま渡せる

タグは json_checker._normalize_tags と同じ正規化（小文字・strip・重複除去）を
通すので、保存時と検索時の表記ブレで取りこぼさない。
"""
import os
import sqlite3
from datetime import datetime, timezone

from json_checker import _normalize_tags

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "agent_memory.db")

# 経験(教訓/スキル/知識)を「関連あり」とみなす類似度の最低ライン。
# 低すぎると無関係な経験まで読み込まれプランナーの文脈が汚れる。
# hashフォールバック埋め込みは文字n-gramの偶発一致でスコアが出やすいため
# 高めの閾値が必要。ollamaの意味埋め込みはより低い閾値で十分。
_REL_FLOOR = {"hash": 0.25, "ollama": 0.35}


def _rel_floor() -> float:
    try:
        from memory.embed import backend_name
        return _REL_FLOOR.get(backend_name(), 0.25)
    except Exception:
        return 0.25


def _gate_relevant(scored, limit):
    """類似度降順の[(sim, row), ...]から、関連性の高いものだけ残す。
    絶対しきい値(_rel_floor)に加え、相対ゲートを掛ける:
    最上位スコアの55%未満の項目は「明らかに関連が薄い」として落とす。
    これにより、たまたま閾値を超えた無関係な経験の混入を防ぐ。"""
    floor = _rel_floor()
    kept = [(s, r) for s, r in scored if s >= floor]
    if not kept:
        return []
    top = kept[0][0]
    rel_cut = top * 0.55
    return [(s, r) for s, r in kept if s >= rel_cut][:limit]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT    NOT NULL,
    goal       TEXT    NOT NULL,
    objective  TEXT    NOT NULL DEFAULT '',
    summary    TEXT    NOT NULL,
    success    INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS memory_tags (
    memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);
CREATE TABLE IF NOT EXISTS entities (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL,
    etype TEXT NOT NULL DEFAULT '',
    UNIQUE(name, etype)
);
CREATE TABLE IF NOT EXISTS relations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    label     TEXT NOT NULL DEFAULT '',
    memory_id INTEGER REFERENCES memories(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, label)
);
CREATE INDEX IF NOT EXISTS idx_rel_src ON relations(source_id);
CREATE TABLE IF NOT EXISTS embeddings (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    vec TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, goal TEXT, lesson TEXT NOT NULL,
    score INTEGER DEFAULT 0, vec TEXT
);
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, name TEXT NOT NULL, description TEXT,
    steps TEXT NOT NULL, tags TEXT, uses INTEGER DEFAULT 0,
    success INTEGER DEFAULT 1, vec TEXT
);
CREATE TABLE IF NOT EXISTS experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, objective TEXT, action TEXT,
    result TEXT, success INTEGER DEFAULT 0, vec TEXT
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, condition TEXT NOT NULL, directive TEXT NOT NULL,
    weight REAL DEFAULT 1.0, uses INTEGER DEFAULT 0,
    source_lesson TEXT, vec TEXT,
    confidence REAL DEFAULT 0.5, success_rate REAL DEFAULT 0.0,
    successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
    last_verified TEXT DEFAULT '', priority INTEGER DEFAULT 0,
    UNIQUE(condition, directive)
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL, goal TEXT, objective TEXT,
    steps TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL, description TEXT,
    success_rate REAL DEFAULT 0.0, successes INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0, uses INTEGER DEFAULT 0,
    last_used TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exploration_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, objective TEXT, hypothesis TEXT, category TEXT,
    score REAL, success INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS exploration_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL, objective TEXT, exploration_depth INTEGER DEFAULT 0,
    unique_hypotheses INTEGER DEFAULT 0, dead_ends INTEGER DEFAULT 0,
    novel_paths INTEGER DEFAULT 0, strategy_switches INTEGER DEFAULT 0
);
"""


# 共通キーワード（このどれかを共有する記憶は「関連」で結ぶ）
_KEYWORDS = ("暗号", "AES", "DES", "RSA", "ハッシュ", "復号", "鍵",
             "プロジェクト", "スキャン", "偵察", "ファイル", "解析",
             "セキュリティ", "ネットワーク", "テスト", "コード")


def _keywords(text: str) -> list[str]:
    """ゴール文字列から、関連付けに使う重要キーワードを拾う。"""
    text = text or ""
    found = [k for k in _KEYWORDS if k in text]
    # 「暗号」を含むものはすべて緩く結ぶ（AES⇔DES⇔RSA等）
    return found


class LongTermMemory:
    def __init__(self, db_path: str = DEFAULT_DB):
        # ThreadingHTTPServerの別スレッドからも使えるように（接続は短命なので安全）
        self.conn = sqlite3.connect(db_path, check_same_thread=False,
                                    timeout=10.0)
        # ロック競合（agent実行中の書き込みとconsolidate等の同時アクセス）に備える:
        # busy_timeoutでロック中は待ってリトライ、WALで読み書き並行性を上げる。
        try:
            self.conn.execute("PRAGMA busy_timeout=10000")
            if db_path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def save(self, goal: str, objective: str, summary: str,
             tags: list[str], success: bool = True) -> int:
        """1つの目的が完了するたびに呼ぶ。保存したレコードのidを返す。"""
        if not summary or not summary.strip():
            raise ValueError("summary が空です")
        tags = _normalize_tags(tags)

        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO memories (created_at, goal, objective, summary, success) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    goal,
                    objective or "",
                    summary,
                    int(success),
                ),
            )
            self.conn.executemany(
                "INSERT INTO memory_tags (memory_id, tag) VALUES (?, ?)",
                [(cur.lastrowid, t) for t in tags],
            )
        mem_id = cur.lastrowid
        self._extract_entities(mem_id, goal, objective, summary)  # 自動でエンティティ/関係抽出
        self._save_embedding(mem_id, f"{goal} {objective} {summary}")  # 意味検索用ベクトル
        return mem_id

    def _save_embedding(self, mem_id: int, text: str) -> None:
        try:
            from memory.embed import embed
            import json as _j
            vec = embed(text)
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO embeddings (memory_id, vec) VALUES (?, ?)",
                    (mem_id, _j.dumps(vec)))
        except Exception:
            pass

    def semantic_search(self, query: str, limit: int = 5) -> list[dict]:
        """意味（ベクトル類似）で関連する記憶を引く。タグ一致より柔軟。"""
        import json as _j
        from memory.embed import embed, cosine
        qv = embed(query)
        rows = self.conn.execute(
            "SELECT m.id, m.goal, m.objective, m.summary, m.success, e.vec "
            "FROM memories m JOIN embeddings e ON m.id=e.memory_id").fetchall()
        scored = []
        for r in rows:
            try:
                sim = cosine(qv, _j.loads(r["vec"]))
            except Exception:
                sim = 0.0
            scored.append((sim, r))
        scored.sort(key=lambda x: -x[0])
        return [{"id": r["id"], "goal": r["goal"], "objective": r["objective"],
                 "summary": r["summary"], "success": bool(r["success"]),
                 "similarity": round(sim, 3)}
                for sim, r in _gate_relevant(scored, limit)]

    # ---- 経験・教訓 ----
    def add_lesson(self, goal: str, lesson: str, score: int = 0) -> None:
        import json as _j, datetime
        from memory.embed import embed
        with self.conn:
            # 同一文言の教訓が既にあれば重複追加しない（スパム防止）。
            # スコアは「より極端な方」を残す（強い成功/強い失敗を優先）。
            existing = self.conn.execute(
                "SELECT id, score FROM lessons WHERE lesson=?", (lesson,)).fetchone()
            if existing:
                if abs(score) > abs(existing["score"]):
                    self.conn.execute("UPDATE lessons SET score=? WHERE id=?",
                                      (score, existing["id"]))
                return
            self.conn.execute(
                "INSERT INTO lessons (created_at, goal, lesson, score, vec) "
                "VALUES (?,?,?,?,?)",
                (datetime.datetime.now().isoformat(), goal, lesson, score,
                 _j.dumps(embed(f"{goal} {lesson}"))))

    def relevant_lessons(self, query: str, limit: int = 3) -> list[dict]:
        """状況に近い過去の教訓を引く（次回計画に活かす＝賢くなる核心）。"""
        import json as _j
        from memory.embed import embed, cosine
        qv = embed(query)
        rows = self.conn.execute(
            "SELECT id, goal, lesson, score, vec FROM lessons").fetchall()
        scored = []
        for r in rows:
            try:
                sim = cosine(qv, _j.loads(r["vec"])) if r["vec"] else 0.0
            except Exception:
                sim = 0.0
            scored.append((sim, r))
        scored.sort(key=lambda x: -x[0])
        gated = _gate_relevant(scored, limit)
        return [{"lesson": r["lesson"], "goal": r["goal"], "score": r["score"],
                 "similarity": round(sim, 3)}
                for sim, r in gated]

    def all_lessons(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, created_at, goal, lesson, score FROM lessons "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 経験（1試行の生データ。経験学習の素材）----
    def add_experience(self, objective: str, action, result: str,
                       success: bool) -> int:
        """1試行の経験を保存。Critic→MemoryManager から呼ばれる。"""
        import json as _j, datetime
        from memory.embed import embed
        act = action if isinstance(action, str) else _j.dumps(
            action, ensure_ascii=False)
        vec = _j.dumps(embed(f"{objective} {act} {result}"))
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO experiences (created_at, objective, action, result, "
                "success, vec) VALUES (?,?,?,?,?,?)",
                (datetime.datetime.now().isoformat(), objective, act,
                 str(result)[:2000], 1 if success else 0, vec))
            return cur.lastrowid

    def recent_experiences(self, limit: int = 20,
                           only_failed: bool = False) -> list[dict]:
        q = ("SELECT id, created_at, objective, action, result, success "
             "FROM experiences ")
        if only_failed:
            q += "WHERE success=0 "
        q += "ORDER BY id DESC LIMIT ?"
        return [dict(r) for r in self.conn.execute(q, (limit,)).fetchall()]

    # ---- ルール（教訓を昇華した自動適用ルール）----
    def add_rule(self, condition: str, directive: str,
                 source_lesson: str = "") -> int:
        """自動適用ルールを保存。既存(condition,directive)は重みを強化。"""
        import json as _j, datetime
        from memory.embed import embed
        self._ensure_rule_columns()
        vec = _j.dumps(embed(f"{condition} {directive}"))
        existing = self.conn.execute(
            "SELECT id, weight FROM rules WHERE condition=? AND directive=?",
            (condition, directive)).fetchone()
        with self.conn:
            if existing:
                self.conn.execute(
                    "UPDATE rules SET weight=weight+0.5 WHERE id=?",
                    (existing["id"],))
                return existing["id"]
            cur = self.conn.execute(
                "INSERT INTO rules (created_at, condition, directive, "
                "source_lesson, vec) VALUES (?,?,?,?,?)",
                (datetime.datetime.now().isoformat(), condition, directive,
                 source_lesson, vec))
            return cur.lastrowid

    def relevant_rules(self, query: str, limit: int = 5,
                       min_confidence: float = 0.35) -> list[dict]:
        """現在の状況に関連する自動適用ルールを引く（関連度＋信頼度フィルタ）。
        信頼度の低いルールは自動適用しない（Phase2: 誤学習の暴走防止）。"""
        import json as _j
        from memory.embed import embed, cosine
        self._ensure_rule_columns()
        qv = embed(query)
        rows = self.conn.execute(
            "SELECT id, condition, directive, weight, uses, vec, "
            "confidence, success_rate, priority FROM rules"
        ).fetchall()
        scored = []
        for r in rows:
            # 信頼度が低いルールは適用候補から除外
            if (r["confidence"] if r["confidence"] is not None else 0.5) < min_confidence:
                continue
            try:
                sim = cosine(qv, _j.loads(r["vec"])) if r["vec"] else 0.0
            except Exception:
                sim = 0.0
            scored.append((sim, r))
        scored.sort(key=lambda x: -x[0])
        gated = _gate_relevant(scored, limit)
        return [{"id": r["id"], "condition": r["condition"],
                 "directive": r["directive"], "weight": r["weight"],
                 "confidence": r["confidence"], "success_rate": r["success_rate"],
                 "priority": r["priority"], "similarity": round(sim, 3)}
                for sim, r in gated]

    def _ensure_rule_columns(self) -> None:
        """旧DBにPhase2のrules列が無ければ追加する（後方互換マイグレーション）。"""
        try:
            cols = {r["name"] for r in self.conn.execute(
                "PRAGMA table_info(rules)").fetchall()}
            adds = {
                "confidence": "REAL DEFAULT 0.5",
                "success_rate": "REAL DEFAULT 0.0",
                "successes": "INTEGER DEFAULT 0",
                "failures": "INTEGER DEFAULT 0",
                "last_verified": "TEXT DEFAULT ''",
                "priority": "INTEGER DEFAULT 0",
            }
            with self.conn:
                for col, ddl in adds.items():
                    if col not in cols:
                        self.conn.execute(f"ALTER TABLE rules ADD COLUMN {col} {ddl}")
        except Exception:
            pass

    def record_rule_outcome(self, rule_id: int, success: bool) -> None:
        """ルール適用の成否を記録し、信頼度・成功率を更新する（Phase2）。"""
        import datetime
        self._ensure_rule_columns()
        try:
            with self.conn:
                if success:
                    self.conn.execute(
                        "UPDATE rules SET successes=successes+1, uses=uses+1 WHERE id=?",
                        (rule_id,))
                else:
                    self.conn.execute(
                        "UPDATE rules SET failures=failures+1, uses=uses+1 WHERE id=?",
                        (rule_id,))
                row = self.conn.execute(
                    "SELECT successes, failures FROM rules WHERE id=?",
                    (rule_id,)).fetchone()
                if row:
                    s, f = row["successes"] or 0, row["failures"] or 0
                    total = s + f
                    rate = s / total if total else 0.0
                    # 信頼度 = 成功率を試行数で平滑化（少試行では中庸に寄せる）
                    conf = (s + 1) / (total + 2)   # ラプラス平滑化
                    self.conn.execute(
                        "UPDATE rules SET success_rate=?, confidence=?, "
                        "last_verified=?, priority=? WHERE id=?",
                        (round(rate, 3), round(conf, 3),
                         datetime.datetime.now().isoformat(),
                         int(conf * 10), rule_id))
        except Exception:
            pass

    def mark_rule_used(self, rule_id: int) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE rules SET uses=uses+1 WHERE id=?", (rule_id,))
        except Exception:
            pass

    def all_rules(self, limit: int = 100) -> list[dict]:
        self._ensure_rule_columns()
        rows = self.conn.execute(
            "SELECT id, created_at, condition, directive, weight, uses, "
            "confidence, success_rate, successes, failures, priority "
            "FROM rules ORDER BY priority DESC, weight DESC, id DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 戦略（Ruleの上位概念。Phase3）----
    def all_strategies(self) -> list[dict]:
        try:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM strategies").fetchall()]
        except Exception:
            return []

    def record_strategy_outcome(self, name: str, success: bool,
                                description: str = "") -> None:
        """戦略の成否を記録し成功率を更新（無ければ作成）。"""
        import datetime
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO strategies (name, description) VALUES (?,?)",
                    (name, description))
                if success:
                    self.conn.execute(
                        "UPDATE strategies SET successes=successes+1, uses=uses+1, "
                        "last_used=? WHERE name=?",
                        (datetime.datetime.now().isoformat(), name))
                else:
                    self.conn.execute(
                        "UPDATE strategies SET failures=failures+1, uses=uses+1, "
                        "last_used=? WHERE name=?",
                        (datetime.datetime.now().isoformat(), name))
                row = self.conn.execute(
                    "SELECT successes, failures FROM strategies WHERE name=?",
                    (name,)).fetchone()
                if row:
                    s, f = row["successes"] or 0, row["failures"] or 0
                    total = s + f
                    rate = round(s / total, 3) if total else 0.0
                    self.conn.execute(
                        "UPDATE strategies SET success_rate=? WHERE name=?",
                        (rate, name))
        except Exception:
            pass

    # ---- 探索履歴・メトリクス（Phase3）----
    def record_exploration(self, objective: str, hypothesis: str,
                           category: str, score: float, success: bool) -> None:
        import time as _t
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO exploration_history "
                    "(created_at, objective, hypothesis, category, score, success) "
                    "VALUES (?,?,?,?,?,?)",
                    (_t.time(), objective, hypothesis[:200], category,
                     float(score), 1 if success else 0))
        except Exception:
            pass

    def save_exploration_metrics(self, objective: str, metrics: dict) -> None:
        import time as _t
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO exploration_metrics "
                    "(created_at, objective, exploration_depth, unique_hypotheses, "
                    "dead_ends, novel_paths, strategy_switches) VALUES (?,?,?,?,?,?,?)",
                    (_t.time(), objective,
                     metrics.get("exploration_depth", 0),
                     metrics.get("unique_hypotheses", 0),
                     metrics.get("dead_ends", 0),
                     metrics.get("novel_paths", 0),
                     metrics.get("strategy_switches", 0)))
        except Exception:
            pass

    def exploration_summary(self, limit: int = 50) -> dict:
        """最近の探索メトリクスを集計（Reflection用）。"""
        try:
            rows = self.conn.execute(
                "SELECT * FROM exploration_metrics ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            if not rows:
                return {}
            agg = {"runs": len(rows)}
            for k in ("exploration_depth", "unique_hypotheses", "dead_ends",
                      "novel_paths", "strategy_switches"):
                agg[k] = sum(r[k] or 0 for r in rows)
            return agg
        except Exception:
            return {}

    # ---- スキル（成功手順の再利用可能パターン）----
    def add_skill(self, name: str, description: str, steps: list[str],
                  tags: list[str] | None = None) -> int:
        """成功した手順を再利用可能なスキルとして保存。同名は手順を更新。"""
        import json as _j, datetime
        from memory.embed import embed
        tags = tags or []
        vec = _j.dumps(embed(f"{name} {description} {' '.join(steps)}"))
        existing = self.conn.execute("SELECT id FROM skills WHERE name=?", (name,)).fetchone()
        with self.conn:
            if existing:
                self.conn.execute(
                    "UPDATE skills SET description=?, steps=?, tags=?, vec=? WHERE id=?",
                    (description, _j.dumps(steps, ensure_ascii=False),
                     _j.dumps(tags, ensure_ascii=False), vec, existing["id"]))
                return existing["id"]
            cur = self.conn.execute(
                "INSERT INTO skills (created_at, name, description, steps, tags, vec) "
                "VALUES (?,?,?,?,?,?)",
                (datetime.datetime.now().isoformat(), name, description,
                 _j.dumps(steps, ensure_ascii=False),
                 _j.dumps(tags, ensure_ascii=False), vec))
            return cur.lastrowid

    def relevant_skills(self, query: str, limit: int = 3) -> list[dict]:
        """状況に近い既存スキルを引く（再利用＝賢くなる核心）。"""
        import json as _j
        from memory.embed import embed, cosine
        qv = embed(query)
        rows = self.conn.execute(
            "SELECT id, name, description, steps, tags, uses, success, vec "
            "FROM skills").fetchall()
        scored = []
        for r in rows:
            try:
                sim = cosine(qv, _j.loads(r["vec"])) if r["vec"] else 0.0
            except Exception:
                sim = 0.0
            scored.append((sim, r))
        scored.sort(key=lambda x: -x[0])
        out = []
        for sim, r in _gate_relevant(scored, limit):
            try:
                steps = _j.loads(r["steps"])
            except Exception:
                steps = []
            out.append({"id": r["id"], "name": r["name"],
                        "description": r["description"], "steps": steps,
                        "uses": r["uses"], "success": r["success"],
                        "similarity": round(sim, 3)})
        return out

    def mark_skill_used(self, skill_id: int, success: bool = True) -> None:
        # 累積で成功回数を加算（昇格判定に使う）。successカラムは累積成功数として扱う。
        with self.conn:
            self.conn.execute(
                "UPDATE skills SET uses = uses + 1, "
                "success = success + ? WHERE id = ?",
                (1 if success else 0, skill_id))

    def all_skills(self, limit: int = 100) -> list[dict]:
        import json as _j
        rows = self.conn.execute(
            "SELECT id, created_at, name, description, steps, tags, uses, success "
            "FROM skills ORDER BY uses DESC, id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["steps"] = _j.loads(r["steps"])
                d["tags"] = _j.loads(r["tags"]) if r["tags"] else []
            except Exception:
                d["steps"], d["tags"] = [], []
            out.append(d)
        return out

    # ---- 記憶整理（重複統合・低品質除去）----
    def consolidate(self) -> dict:
        """記憶を整理：①完全重複の要約を削除 ②orphanエンティティ掃除。"""
        removed_dup = 0
        with self.conn:
            # 同じ goal+objective+summary の重複を1件に
            rows = self.conn.execute(
                "SELECT id, goal, objective, summary FROM memories ORDER BY id").fetchall()
            seen = {}
            for r in rows:
                key = (r["goal"], r["objective"], r["summary"])
                if key in seen:
                    self.conn.execute("DELETE FROM memories WHERE id=?", (r["id"],))
                    self.conn.execute("DELETE FROM embeddings WHERE memory_id=?", (r["id"],))
                    removed_dup += 1
                else:
                    seen[key] = r["id"]
            # どの関係にも使われていないエンティティを削除
            self.conn.execute(
                "DELETE FROM entities WHERE id NOT IN "
                "(SELECT source_id FROM relations UNION SELECT target_id FROM relations)")
            # 重複教訓の削除（同じ lesson 文）
            lrows = self.conn.execute("SELECT id, lesson FROM lessons ORDER BY id").fetchall()
            seen_l = set()
            removed_lessons = 0
            for r in lrows:
                if r["lesson"] in seen_l:
                    self.conn.execute("DELETE FROM lessons WHERE id=?", (r["id"],))
                    removed_lessons += 1
                else:
                    seen_l.add(r["lesson"])
        return {"removed_duplicates": removed_dup, "removed_lessons": removed_lessons}

    # ------------------------------------------------------------------ #
    # エンティティ/関係の保存（ナレッジグラフ用）
    # ------------------------------------------------------------------ #
    def _upsert_entity(self, name: str, etype: str) -> int:
        name = (name or "").strip()
        if not name:
            return 0
        etype = (etype or "").strip()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO entities (name, etype) VALUES (?, ?)",
                (name, etype))
        row = self.conn.execute(
            "SELECT id FROM entities WHERE name=? AND etype=?", (name, etype)).fetchone()
        return row["id"] if row else 0

    def add_relation(self, source: str, s_type: str, target: str, t_type: str,
                     label: str, memory_id: int | None = None) -> None:
        sid = self._upsert_entity(source, s_type)
        tid = self._upsert_entity(target, t_type)
        if not sid or not tid or sid == tid:
            return
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO relations (source_id, target_id, label, memory_id) "
                "VALUES (?, ?, ?, ?)", (sid, tid, label or "", memory_id))

    def _extract_entities(self, mem_id: int, goal: str, objective: str,
                          summary: str) -> None:
        """要約をLLMに渡してエンティティ/関係を抽出し保存。失敗しても保存自体は壊さない。"""
        try:
            from memory.extractor import extract
            data = extract(goal, objective, summary)
        except Exception:
            return
        # Memoryノード（その記憶自体）を中心に、抽出物をぶら下げる
        mem_name = f"Human: {goal}"[:40]
        for ent in data.get("entities", []):
            nm, ty = ent.get("name"), ent.get("type", "")
            if nm:
                self.add_relation(mem_name, "Memory", nm, ty, "抽出元", mem_id)
        for rel in data.get("relations", []):
            self.add_relation(rel.get("source", ""), rel.get("source_type", ""),
                              rel.get("target", ""), rel.get("target_type", ""),
                              rel.get("label", "関係"), mem_id)

    def search_by_tags(self, tags: list[str], limit: int = 5) -> list[dict]:
        """タグが1つでも一致する記憶を、一致数の多い順 → 新しい順で返す。"""
        tags = _normalize_tags(tags)
        if not tags:
            return []

        placeholders = ",".join("?" * len(tags))
        rows = self.conn.execute(
            f"""
            SELECT m.*, COUNT(t.tag) AS hits
            FROM memories m
            JOIN memory_tags t ON t.memory_id = m.id
            WHERE t.tag IN ({placeholders})
            GROUP BY m.id
            ORDER BY hits DESC, m.id DESC
            LIMIT ?
            """,
            (*tags, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def related_knowledge(self, text: str, limit: int = 8) -> list[str]:
        """text に含まれる語に関係するエンティティ・関係を知識グラフから引く。
        AESを調べる時に『AESは対称鍵暗号』『DESも対称鍵暗号』等の関連知識をLLMへ渡す。"""
        text = text or ""
        ents = self.conn.execute("SELECT id, name, etype FROM entities").fetchall()
        id2name = {e["id"]: e["name"] for e in ents}
        # text に名前が含まれるエンティティを起点にする
        seeds = [e for e in ents if e["name"] and e["name"] in text]
        if not seeds:
            return []
        seed_ids = {e["id"] for e in seeds}
        seed_names = {e["name"] for e in seeds}
        # 起点と同名の全ID（名前統合）も種に含める
        for e in ents:
            if e["name"] in seed_names:
                seed_ids.add(e["id"])
        rels = self.conn.execute(
            "SELECT source_id, target_id, label FROM relations").fetchall()
        lines, seen = [], set()
        # 1ホップ：起点に直接つながる関係
        hubs = set()
        for r in rels:
            s_, t_ = r["source_id"], r["target_id"]
            if s_ in seed_ids or t_ in seed_ids:
                sn, tn = id2name.get(s_), id2name.get(t_)
                if not sn or not tn or sn.startswith("Human:") or tn.startswith("Human:"):
                    # Memoryノード経由はハブ抽出のみに使う
                    if sn and not sn.startswith("Human:"):
                        hubs.add(sn)
                    if tn and not tn.startswith("Human:"):
                        hubs.add(tn)
                    continue
                key = (sn, r["label"], tn)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"{sn} —{r['label']}→ {tn}")
                hubs.add(sn); hubs.add(tn)
        # 2ホップ：共有ハブにつながる別の知識（AES⇔対称鍵暗号⇔DES）
        hub_ids = {e["id"] for e in ents if e["name"] in hubs}
        for r in rels:
            sn, tn = id2name.get(r["source_id"]), id2name.get(r["target_id"])
            if not sn or not tn or sn.startswith("Human:") or tn.startswith("Human:"):
                continue
            if r["source_id"] in hub_ids or r["target_id"] in hub_ids:
                key = (sn, r["label"], tn)
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"{sn} —{r['label']}→ {tn}")
            if len(lines) >= limit:
                break
        return lines[:limit]

    def as_context_lines(self, tags: list[str], limit: int = 5) -> list[str]:
        """ShortTermMemory.build_context の related_memories にそのまま渡せる形。"""
        return [
            f"{r['objective'] or r['goal']}: {r['summary']}"
            f"（{'成功' if r['success'] else '失敗'}）"
            for r in self.search_by_tags(tags, limit)
        ]

    def all(self, limit: int = 200) -> list[dict]:
        """保存済みの記憶を新しい順に全件返す（タグ付き）。"""
        rows = self.conn.execute(
            "SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            tags = [t["tag"] for t in self.conn.execute(
                "SELECT tag FROM memory_tags WHERE memory_id=?", (r["id"],))]
            d = dict(r); d["tags"] = tags; d["success"] = bool(d["success"])
            out.append(d)
        return out

    def clear(self) -> None:
        """全記憶・エンティティ・関係を消去（銀河をリセット）。"""
        with self.conn:
            for t in ("relations", "entities", "memory_tags", "memories"):
                self.conn.execute(f"DELETE FROM {t}")

    def graph(self, limit: int = 2000) -> dict:
        """ナレッジグラフ。エンティティは名前で統合し、共有ハブで知識をつなぐ。"""
        ents = self.conn.execute("SELECT id, name, etype FROM entities").fetchall()
        rels = self.conn.execute(
            "SELECT source_id, target_id, label FROM relations").fetchall()

        # --- 1) 名前で統合: 同じ名前は1ノードに（type違いの重複を解消） ---
        name_to_node: dict[str, int] = {}    # name -> 代表ノードID（連番）
        id_to_name: dict[int, str] = {}      # entities.id -> name
        node_meta: dict[int, dict] = {}      # 代表ID -> {name,type,deg}
        next_id = 1
        for e in ents:
            id_to_name[e["id"]] = e["name"]
            if e["name"] not in name_to_node:
                name_to_node[e["name"]] = next_id
                # Memoryタイプは束ねず、純粋なエンティティを優先表示
                node_meta[next_id] = {"name": e["name"],
                                      "type": e["etype"] or "その他", "deg": 0}
                next_id += 1
            else:
                nid = name_to_node[e["name"]]
                if node_meta[nid]["type"] in ("", "その他") and e["etype"]:
                    node_meta[nid]["type"] = e["etype"]

        # --- 2) 関係も名前ベースに張り直し、重複辺は1本に集約 ---
        edge_set: dict = {}     # (src_name, tgt_name, label) -> count
        for r in rels:
            sn = id_to_name.get(r["source_id"]); tn = id_to_name.get(r["target_id"])
            if not sn or not tn or sn == tn:
                continue
            key = (sn, tn, r["label"])
            edge_set[key] = edge_set.get(key, 0) + 1

        edges = []
        for (sn, tn, label), cnt in edge_set.items():
            s, t = name_to_node[sn], name_to_node[tn]
            edges.append({"source": s, "target": t, "label": label, "weight": cnt})
            node_meta[s]["deg"] += 1
            node_meta[t]["deg"] += 1

        nodes = [{"id": nid, **meta} for nid, meta in node_meta.items()]

        # --- 3) 種類別の集計（凡例用。Memoryは除外） ---
        types: dict[str, int] = {}
        for n in nodes:
            if n["type"] == "Memory":
                continue
            types[n["type"]] = types.get(n["type"], 0) + 1

        return {"nodes": nodes, "edges": edges, "types": types}

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def recall(self, query: str, tags: list[str] = None,
               limit: int = 3) -> dict:
        """統合想起: 異種ストア(lessons/skills/vector/KG)を単一IFで横断検索する。
        plannerが「この目的に関連する全記憶」を1回で引けるようにする。
        返り値: {lessons, skills, similar, knowledge}
          lessons   : 関連教訓（成功は活かす/失敗は避ける）
          skills    : 再利用候補スキル（昇格段階付き）
          similar   : 意味的に近い過去経験
          knowledge : 知識グラフから辿った関連事実
        各ストアの失敗は握り潰し、取れたものだけ返す（堅牢性優先）。"""
        out = {"lessons": [], "skills": [], "similar": [], "knowledge": []}
        try:
            out["lessons"] = self.relevant_lessons(query, limit=limit)
        except Exception:
            pass
        try:
            skills = self.relevant_skills(query, limit=limit)
            # 昇格段階で並べ替え（trusted優先）
            try:
                import skill_system
                out["skills"] = skill_system.rank_skills(skills)
            except Exception:
                out["skills"] = skills
        except Exception:
            pass
        try:
            out["similar"] = self.semantic_search(query, limit=limit)
        except Exception:
            pass
        try:
            out["knowledge"] = self.related_knowledge(query, limit=8)
        except Exception:
            pass
        # tag指定があればタグ検索も混ぜる
        if tags:
            try:
                tagged = self.search_by_tags(tags, limit=limit)
                seen = {s.get("id") for s in out["similar"]}
                for t in tagged:
                    if t.get("id") not in seen:
                        out["similar"].append(t)
            except Exception:
                pass
        return out

    def close(self) -> None:
        self.conn.close()


if __name__ == "__main__":
    # 一時DB（メモリ上）で動作確認。実ファイルは汚さない
    ltm = LongTermMemory(":memory:")

    ltm.save(goal="ログ解析レポート作成", objective="ログ形式を調べる",
             summary="ログはJSON Lines形式で logs/ にあった", tags=["Log", "python", "log"])
    ltm.save(goal="ログ解析レポート作成", objective="解析する",
             summary="エラーの8割がタイムアウト起因", tags=["log", "error"])
    ltm.save(goal="バックアップ自動化", objective="スクリプト作成",
             summary="robocopyで毎日3時に実行", tags=["backup", "windows"])

    print("件数:", ltm.count())
    print("--- search ['log', 'error'] ---")
    for r in ltm.search_by_tags(["log", "error"]):
        print(f"  id={r['id']} hits={r['hits']} {r['objective']}: {r['summary']}")
    print("--- as_context_lines ['log'] ---")
    for line in ltm.as_context_lines(["log"]):
        print(" -", line)
    print("--- 一致なし ---")
    print(ltm.search_by_tags(["unknown"]))
    ltm.close()
