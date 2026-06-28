# -*- coding: utf-8 -*-
"""Phase5: Loop Detector.

同一の Hypothesis / Command / Strategy / Dead End の反復を検知し、
閾値超過で loop_detected を示すシグナルを返す。agent_loop が利用する。

純粋なインメモリ判定（状態は run 単位）。DB非依存・例外安全。
"""

from __future__ import annotations

import hashlib
from collections import deque
from typing import Optional


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _key(s: str) -> str:
    return hashlib.md5(_norm(s).encode("utf-8", "ignore")).hexdigest()[:12]


class LoopDetector:
    """1 run 分の反復検知器。

    record_* を呼ぶたびに連続/累積回数を更新し、閾値を超えると
    (True, kind, detail) を返す。閾値未満なら (False, "", "")。
    """

    def __init__(self,
                 cmd_threshold: int = 3,
                 hyp_threshold: int = 3,
                 strat_threshold: int = 4,
                 deadend_threshold: int = 2,
                 window: int = 12):
        self.cmd_threshold = cmd_threshold
        self.hyp_threshold = hyp_threshold
        self.strat_threshold = strat_threshold
        self.deadend_threshold = deadend_threshold
        self._cmd_recent = deque(maxlen=window)
        self._hyp_recent = deque(maxlen=window)
        self._strat_run = []          # 直近の戦略列（連続反復を見る）
        self._deadend_count: dict = {}
        self._last_cmd = ""
        self._last_hyp = ""
        self._cmd_streak = 0
        self._hyp_streak = 0

    # ---- Command ---- #
    def record_command(self, command: str):
        k = _key(command)
        self._cmd_recent.append(k)
        if k == self._last_cmd:
            self._cmd_streak += 1
        else:
            self._cmd_streak = 1
            self._last_cmd = k
        # 連続 or window内多数
        cnt = self._cmd_recent.count(k)
        if self._cmd_streak >= self.cmd_threshold or cnt >= self.cmd_threshold + 1:
            return (True, "command",
                    f"同一コマンドの反復（連続{self._cmd_streak}回/直近{cnt}回）: "
                    f"{_norm(command)[:80]}")
        return (False, "", "")

    # ---- Hypothesis ---- #
    def record_hypothesis(self, hypothesis: str):
        if not hypothesis:
            return (False, "", "")
        k = _key(hypothesis)
        self._hyp_recent.append(k)
        if k == self._last_hyp:
            self._hyp_streak += 1
        else:
            self._hyp_streak = 1
            self._last_hyp = k
        cnt = self._hyp_recent.count(k)
        if self._hyp_streak >= self.hyp_threshold or cnt >= self.hyp_threshold + 1:
            return (True, "hypothesis",
                    f"同一仮説の反復（連続{self._hyp_streak}回/直近{cnt}回）: "
                    f"{_norm(hypothesis)[:80]}")
        return (False, "", "")

    # ---- Strategy ---- #
    def record_strategy(self, strategy: str):
        if not strategy:
            return (False, "", "")
        self._strat_run.append(_key(strategy))
        # 末尾の連続同一数
        streak = 1
        for i in range(len(self._strat_run) - 2, -1, -1):
            if self._strat_run[i] == self._strat_run[-1]:
                streak += 1
            else:
                break
        if streak >= self.strat_threshold:
            return (True, "strategy",
                    f"戦略が{streak}手連続で切り替わっていない: {_norm(strategy)[:60]}")
        return (False, "", "")

    # ---- Dead End ---- #
    def record_dead_end(self, path: str):
        if not path:
            return (False, "", "")
        k = _key(path)
        self._deadend_count[k] = self._deadend_count.get(k, 0) + 1
        if self._deadend_count[k] >= self.deadend_threshold:
            return (True, "dead_end",
                    f"行き止まりへの再突入（{self._deadend_count[k]}回）: "
                    f"{_norm(path)[:80]}")
        return (False, "", "")

    def reset_streaks(self):
        """有効な前進があったときに連続カウンタを緩める。"""
        self._cmd_streak = 0
        self._hyp_streak = 0
        self._last_cmd = ""
        self._last_hyp = ""
