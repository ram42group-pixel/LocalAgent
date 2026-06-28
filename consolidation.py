# -*- coding: utf-8 -*-
#consolidation.py — 記憶の蒸留と圧縮（睡眠/夢に相当するバックグラウンドパス）
"""
「使うほど賢く」を「使うほど肥大」にしないための統合処理。
LTM.consolidate()（重複削除）の上位に、higher-order な蒸留を載せる:

  ① ログ→教訓:    成功/失敗の記憶から繰り返し現れるパターンを教訓化
  ② 教訓→スキル:  高スコア教訓が反復・類似したらスキルへ昇華
  ③ 剪定:        低スコアで古い教訓、未使用candidate skillを間引く

長期運用で記憶が線形増加するのを抑え、要点だけを濃縮して残す。
"""
from __future__ import annotations

import re
from collections import Counter


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _keywords(text: str, n: int = 4) -> list[str]:
    """教訓文から特徴語を粗く抽出（クラスタリングの種）。"""
    words = re.findall(r"[a-zA-Z]{3,}|[ぁ-んァ-ヶ一-龠]{2,}", text or "")
    stop = {"する", "した", "して", "ない", "こと", "ため", "次の", "前の", "the", "and"}
    words = [w for w in words if w not in stop]
    return [w for w, _ in Counter(words).most_common(n)]


def distill_lessons_to_skills(ltm, min_cluster: int = 3,
                              min_score: int = 1) -> dict:
    """高スコア教訓が同じ特徴語で複数集まったら、スキル候補へ昇華する。
    返り値: {promoted: [skill名...], clusters: n}"""
    lessons = ltm.all_lessons(limit=500)
    # 正スコアの教訓のみ対象（失敗教訓=負スコアはスキル化しない）
    pos = [l for l in lessons if l.get("score", 0) >= min_score]
    # 特徴語の組でクラスタリング
    clusters = {}
    for l in pos:
        key = tuple(sorted(_keywords(l["lesson"]))[:2])
        if not key:
            continue
        clusters.setdefault(key, []).append(l)
    promoted = []
    for key, group in clusters.items():
        if len(group) < min_cluster:
            continue
        # クラスタ内の教訓をステップ化してスキル候補に
        name = "学習スキル: " + " ".join(key)
        steps = []
        for l in group[:5]:
            s = re.sub(r"。.*$", "", l["lesson"])[:60]  # 先頭文をステップに
            if s and s not in steps:
                steps.append(s)
        desc = f"{len(group)}件の教訓から自動抽出された手順パターン"
        try:
            ltm.add_skill(name, desc, steps, tags=list(key))
            promoted.append(name)
        except Exception:
            pass
    return {"promoted": promoted, "clusters": len(clusters)}


def prune_memory(ltm, lesson_floor: int = -2,
                 max_lessons: int = 300) -> dict:
    """低価値・過剰な記憶を間引く。
    - score が床値以下の教訓を削除（役に立たなかった失敗の山）
    - 教訓が上限超なら低スコア古い順に削除
    - 未使用candidate skill（uses=0）が大量なら間引き
    返り値: {pruned_lessons, pruned_skills}"""
    pruned_l = 0
    pruned_s = 0
    try:
        with ltm.conn:
            # 床値以下の教訓を削除
            cur = ltm.conn.execute(
                "DELETE FROM lessons WHERE score <= ?", (lesson_floor,))
            pruned_l += cur.rowcount or 0
            # 上限超過なら低スコア・古い順に削除
            cnt = ltm.conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
            if cnt > max_lessons:
                over = cnt - max_lessons
                ltm.conn.execute(
                    "DELETE FROM lessons WHERE id IN "
                    "(SELECT id FROM lessons ORDER BY score ASC, id ASC LIMIT ?)",
                    (over,))
                pruned_l += over
            # 未使用candidate skillが多すぎる場合の間引き（uses=0を古い順に）
            scnt = ltm.conn.execute(
                "SELECT COUNT(*) FROM skills WHERE uses=0").fetchone()[0]
            if scnt > 50:
                ltm.conn.execute(
                    "DELETE FROM skills WHERE id IN "
                    "(SELECT id FROM skills WHERE uses=0 ORDER BY id ASC LIMIT ?)",
                    (scnt - 50,))
                pruned_s += scnt - 50
    except Exception:
        pass
    return {"pruned_lessons": pruned_l, "pruned_skills": pruned_s}


def merge_similar_skills(ltm, threshold: float = 0.85) -> dict:
    """類似スキルを統合する（重複したスキルの乱立を防ぐ）。
    名前/ステップ/埋め込みが近いスキルを1つにまとめ、使用統計を合算する。
    残すのは「より昇格した（成功率×試行回数が高い）方」。
    返り値: {merged: 統合した数}"""
    import json as _j
    try:
        rows = ltm.conn.execute(
            "SELECT id, name, description, steps, tags, uses, success, vec "
            "FROM skills").fetchall()
    except Exception:
        return {"merged": 0}
    skills = []
    for r in rows:
        try:
            vec = _j.loads(r["vec"]) if r["vec"] else None
        except Exception:
            vec = None
        try:
            steps = _j.loads(r["steps"]) if r["steps"] else []
        except Exception:
            steps = []
        skills.append({"id": r["id"], "name": r["name"], "steps": steps,
                       "uses": r["uses"] or 0, "success": r["success"] or 0,
                       "vec": vec})

    def cos(a, b):
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        dot = sum(a[i] * b[i] for i in range(n))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    def step_overlap(s1, s2):
        a, b = set(s1), set(s2)
        return len(a & b) / len(a | b) if (a | b) else 0.0

    merged = 0
    removed = set()
    for i in range(len(skills)):
        if skills[i]["id"] in removed:
            continue
        for j in range(i + 1, len(skills)):
            if skills[j]["id"] in removed:
                continue
            si, sj = skills[i], skills[j]
            sim = max(cos(si["vec"], sj["vec"]),
                      step_overlap(si["steps"], sj["steps"]))
            if sim >= threshold:
                # 昇格度（成功率×試行）が高い方を残す
                score_i = (si["success"] / si["uses"] if si["uses"] else 0) * si["uses"]
                score_j = (sj["success"] / sj["uses"] if sj["uses"] else 0) * sj["uses"]
                keep, drop = (si, sj) if score_i >= score_j else (sj, si)
                # 使用統計を合算して keep に集約
                try:
                    with ltm.conn:
                        ltm.conn.execute(
                            "UPDATE skills SET uses=?, success=? WHERE id=?",
                            (keep["uses"] + drop["uses"],
                             keep["success"] + drop["success"], keep["id"]))
                        ltm.conn.execute("DELETE FROM skills WHERE id=?",
                                         (drop["id"],))
                    keep["uses"] += drop["uses"]
                    keep["success"] += drop["success"]
                    removed.add(drop["id"])
                    merged += 1
                except Exception:
                    pass
    return {"merged": merged}


def run_full(ltm) -> dict:
    """フル統合パス: 重複削除 → 教訓→スキル昇華 → 類似スキル統合 → 剪定。
    定期的（実行N回ごと / 手動）に呼ぶ。"""
    base = {}
    try:
        base = ltm.consolidate()      # 既存: 重複削除＋orphan掃除
    except Exception:
        pass
    distilled = distill_lessons_to_skills(ltm)
    merged = merge_similar_skills(ltm)        # 類似スキル統合
    pruned = prune_memory(ltm)
    return {**base, **distilled, **merged, **pruned}
