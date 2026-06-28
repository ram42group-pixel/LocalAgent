# -*- coding: utf-8 -*-
#core/exploration_engine.py — 探索エンジン（Phase3）
"""
Fact駆動からExploration駆動へ。仮説を confidence だけで選ばず、
新規性(novelty)・多様性(diversity)・反復penalty を加味して選ぶことで、
同じ失敗経路への固執（局所最適化）を防ぐ。

スコア:
  score = confidence
        + novelty_bonus        # 未探索カテゴリ/未試行仮説へ加点
        + diversity_bonus      # 直近選んだカテゴリと異なると加点
        - repetition_penalty   # 同経路の失敗回数に応じ減点

責務:
  - 仮説選択（select_hypothesis）
  - 探索済み経路管理（World State連携）
  - 多様性管理（カテゴリの偏り是正）
  - 行き止まり判定（dead-end detection）
  - 探索予算管理（exploration budget）
"""
from __future__ import annotations
import re


# 仮説カテゴリの推定キーワード（多様性スコアの軸）
_CATEGORY_KEYWORDS = {
    "cve": ["cve", "脆弱性", "exploit", "既知", "searchsploit"],
    "config": ["設定", "config", "デフォルト", "既定", "情報漏洩", "misconfig"],
    "upload": ["upload", "アップロード", "ファイル送信", "ファイルアップロード"],
    "auth": ["認証", "auth", "ログイン", "credential", "資格情報", "brute", "弱い", "パスワード"],
    "session": ["session", "セッション", "cookie", "token", "jwt"],
    "api": ["api", "endpoint", "エンドポイント", "rest", "graphql"],
    "backup": ["backup", "バックアップ", ".bak", "古い", "アーカイブ"],
    "content": ["ディレクトリ", "gobuster", "ffuf", "コンテンツ探索", "列挙"],
    "recon": ["偵察", "scan", "スキャン", "ポート"],
}


def infer_category(text: str) -> str:
    """仮説テキストからカテゴリを推定する（多様性管理用）。"""
    t = (text or "").lower()
    best, best_n = "other", 0
    for cat, kws in _CATEGORY_KEYWORDS.items():
        n = sum(1 for k in kws if k.lower() in t)
        if n > best_n:
            best, best_n = cat, n
    return best


class ExplorationEngine:
    """探索エンジン。仮説をスコアリングして次に試すものを選ぶ。"""

    # スコア係数（チューニング可能）
    W_NOVELTY = 0.4
    W_DIVERSITY = 0.3
    W_REPETITION = 0.25
    DEAD_END_FAILS = 3        # 同経路の失敗がこの回数で行き止まり

    def __init__(self, world=None, budget: dict = None):
        self._world = world
        self.budget = {"max_attempts": 20, "max_dead_ends": 5}
        if budget:
            self.budget.update(budget)
        # 直近に選んだカテゴリ履歴（多様性スコア用）
        self._recent_categories: list[str] = []
        # 経路ごとの失敗回数（反復penalty/行き止まり判定用）
        self._fail_counts: dict[str, int] = {}
        # メトリクス（要件9）
        self.metrics = {"exploration_depth": 0, "unique_hypotheses": 0,
                        "dead_ends": 0, "novel_paths": 0, "strategy_switches": 0}
        self._seen_hyps: set = set()

    # --- 経路キー（仮説の主たる検証手）---
    def _path_key(self, hyp: dict) -> str:
        steps = hyp.get("next_steps") or []
        base = steps[0] if steps else hyp.get("description", "")
        # コマンドの主要部だけを正規化（引数違いを同一視）
        return re.sub(r"\s+", " ", str(base).lower()).strip()[:120]

    # --- 反復penalty: 経路の失敗回数に応じて減点 ---
    def _repetition_penalty(self, hyp: dict) -> float:
        key = self._path_key(hyp)
        fails = self._fail_counts.get(key, 0)
        # World Stateのtested/dead_endも加味
        if self._world is not None:
            try:
                if self._world.is_tested(key):
                    fails += 1
                if key in set(self._world.dead_ends()):
                    fails += self.DEAD_END_FAILS
            except Exception:
                pass
        return self.W_REPETITION * min(4, fails)

    # --- novelty: 未試行ほど加点 ---
    def _novelty_bonus(self, hyp: dict) -> float:
        key = self._path_key(hyp)
        tested = False
        if self._world is not None:
            try:
                tested = self._world.is_tested(key)
            except Exception:
                tested = False
        if key in self._fail_counts:
            tested = True
        return 0.0 if tested else self.W_NOVELTY

    # --- diversity: 直近と違うカテゴリほど加点 ---
    def _diversity_bonus(self, hyp: dict) -> float:
        cat = hyp.get("category") or infer_category(hyp.get("description", ""))
        if not self._recent_categories:
            return 0.0
        # 直近3手で選んだカテゴリに含まれていなければ加点
        recent = self._recent_categories[-3:]
        if cat not in recent:
            return self.W_DIVERSITY
        # 直近が全部同一カテゴリなら、同じものへは強めの減点的扱い
        if recent.count(cat) >= 3:
            return -self.W_DIVERSITY
        return 0.0

    def score(self, hyp: dict) -> dict:
        """仮説の総合探索スコアを返す（内訳つき）。"""
        conf = float(hyp.get("confidence", 0.0) or 0.0)
        nov = self._novelty_bonus(hyp)
        div = self._diversity_bonus(hyp)
        rep = self._repetition_penalty(hyp)
        total = conf + nov + div - rep
        return {"total": round(total, 3), "confidence": conf,
                "novelty": round(nov, 3), "diversity": round(div, 3),
                "repetition_penalty": round(rep, 3)}

    def select_hypothesis(self, hypotheses: list[dict],
                          commit: bool = True) -> dict | None:
        """探索スコア最大の仮説を選ぶ。confidenceのみには依らない。
        行き止まり化した仮説は除外する。
        commit=False のときは状態を変えない（計画文脈の表示用プレビュー）。
        commit=True のときのみメトリクス・カテゴリ履歴を更新する。"""
        if not hypotheses:
            return None
        ranked = []
        for h in hypotheses:
            # 行き止まり済みは候補から外す
            if self.is_dead_end(h):
                continue
            sc = self.score(h)
            ranked.append((sc["total"], sc, h))
        if not ranked:
            return None
        ranked.sort(key=lambda x: -x[0])
        _, best_sc, best = ranked[0]
        best = dict(best)
        best["_score"] = best_sc
        if not commit:
            return best       # プレビュー: 状態を変えない
        # 選択を確定（多様性履歴・メトリクス更新）
        cat = best.get("category") or infer_category(best.get("description", ""))
        self._recent_categories.append(cat)
        key = self._path_key(best)
        if key not in self._seen_hyps:
            self._seen_hyps.add(key)
            self.metrics["unique_hypotheses"] += 1
            if best_sc["novelty"] > 0:
                self.metrics["novel_paths"] += 1
        self.metrics["exploration_depth"] += 1
        return best

    def peek_hypothesis(self, hypotheses: list[dict]) -> dict | None:
        """状態を変えずに、今選ばれるであろう仮説を返す（計画文脈表示用）。"""
        return self.select_hypothesis(hypotheses, commit=False)

    # --- 結果の取り込み（失敗回数の更新） ---
    def record_result(self, hyp: dict, success: bool) -> None:
        key = self._path_key(hyp)
        if not success:
            self._fail_counts[key] = self._fail_counts.get(key, 0) + 1
            if self._fail_counts[key] == self.DEAD_END_FAILS:
                self.metrics["dead_ends"] += 1
                if self._world is not None:
                    try:
                        self._world.mark_dead_end(key, "同一経路で3回失敗")
                    except Exception:
                        pass
        else:
            # 成功した経路はカウンタをリセット
            self._fail_counts.pop(key, None)
        if self._world is not None:
            try:
                self._world.mark_tested(key, "ok" if success else "fail")
            except Exception:
                pass

    # --- 行き止まり判定 ---
    def is_dead_end(self, hyp: dict) -> bool:
        key = self._path_key(hyp)
        if self._fail_counts.get(key, 0) >= self.DEAD_END_FAILS:
            return True
        if self._world is not None:
            try:
                if key in set(self._world.dead_ends()):
                    return True
            except Exception:
                pass
        return False

    def stuck(self, new_facts: int = 0) -> bool:
        """行き止まり状態か（新規Fact無し＋失敗多発）を判定。
        Trueなら呼び出し側は仮説再生成や戦略変更を行う。"""
        many_dead = self.metrics["dead_ends"] >= 1 and new_facts == 0
        return many_dead

    # --- 探索予算 ---
    def budget_exceeded(self) -> bool:
        return (self.metrics["exploration_depth"] >= self.budget["max_attempts"]
                or self.metrics["dead_ends"] >= self.budget["max_dead_ends"])

    def note_strategy_switch(self) -> None:
        self.metrics["strategy_switches"] += 1
