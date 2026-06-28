# -*- coding: utf-8 -*-
#short_term.py — 短期記憶（ワーキングメモリ）
"""
「会話履歴を全部持つ」のではなく「毎ターンのコンテキストを組み立てる」方式。
system.txt が想定する [最終ゴール][今の目的][関連記憶] に加えて
[これまでの行動と結果] を生成して system 役に渡す。

  stm = ShortTermMemory(max_records=5)
  stm.set_goal(goal_data)          # handle_goal の戻り値をそのまま渡す
  stm.add_record(action, result)   # systemの行動JSONと実行結果を1手ずつ追加
  stm.build_context(related)       # → systemに渡すコンテキスト文字列
  stm.advance()                    # 今の目的が完了したら次へ
"""
from collections import deque
from dataclasses import dataclass

CARRYOVER_LIMIT = 1000  # 押し出し要約の保持上限（文字）。無限に育つのを防ぐ


def is_failure_result(result) -> bool:
    """実行結果が失敗・無効を示すか判定（誤検知を避けるため先頭・明確語で判断）。
    「0 errors」「0 failures」のような成功出力を失敗と誤判定しないようにする。"""
    if not isinstance(result, str):
        return False
    r = result.strip()
    # 明確な失敗マーカー（先頭がエラー、または日本語の明示的な失敗・未実行表現）
    if r.startswith("エラー") or r.startswith("Error") or r.startswith("error"):
        return True
    markers = ("が存在しない", "見つからない", "未実行", "導入失敗",
               "実行を拒否", "LOOP DETECTED", "タイムアウト")
    if any(m in r for m in markers):
        return True
    # 英語のNo such file / not found（コマンド出力の典型的な失敗）
    low = r.lower()
    if "no such file" in low or "command not found" in low:
        return True
    return False


@dataclass
class ActionRecord:
    action: dict  # systemが出した行動JSON（handle_taskの戻り値）
    result: str   # 実行結果（コマンド出力・成否など）


class ShortTermMemory:
    def __init__(self, max_records: int = 5, summarizer=None):
        """
        max_records: 覚えておく直近の行動数。超えた分は古い順に押し出す。
        summarizer:  押し出すとき呼ぶ callable(text: str) -> str（任意）。
                     LLMによる要約を差し込める。未指定なら1行ログとして残す。
        """
        self.goal: str = ""
        self.objectives: list[str] = []
        self.tags: list[str] = []
        self.index: int = 0
        self.records: deque[ActionRecord] = deque()
        self.max_records = max_records
        self.summarizer = summarizer
        self.carryover: str = ""  # 押し出された古い履歴の要約

    # ------------------------------------------------------------------ #
    # goal（handle_goalの戻り値）を受け取って初期化
    # ------------------------------------------------------------------ #
    def set_goal(self, goal_data: dict) -> None:
        self.goal = goal_data["goal"]
        self.objectives = list(goal_data["objectives"])
        self.tags = list(goal_data.get("tags", []))
        self.index = 0
        self.records.clear()
        self.carryover = ""

    @property
    def current_objective(self) -> str | None:
        if 0 <= self.index < len(self.objectives):
            return self.objectives[self.index]
        return None

    @property
    def finished(self) -> bool:
        return self.index >= len(self.objectives)

    def finish(self) -> None:
        """全目的を完了済みにする（停止要求時などに run ループを抜けるため）。
        finished は計算プロパティなので、index を末尾へ進めて達成させる。"""
        self.index = len(self.objectives)

    # ------------------------------------------------------------------ #
    # 行動の記録（上限を超えた分は要約に畳む）
    # ------------------------------------------------------------------ #
    def add_record(self, action: dict, result: str) -> None:
        self.records.append(ActionRecord(action=action, result=result))
        while len(self.records) > self.max_records:
            self._evict(self.records.popleft())

    def _evict(self, record: ActionRecord) -> None:
        line = (
            f"{record.action.get('type')}: "
            f"{record.action.get('reason', '')} → {record.result}"
        )
        merged = "\n".join(filter(None, [self.carryover, line]))
        if self.summarizer:
            self.carryover = self.summarizer(merged)
        else:
            self.carryover = merged[-CARRYOVER_LIMIT:]

    def advance(self) -> bool:
        """今の目的を完了して次へ進む。次の目的があれば True。"""
        self.index += 1
        self.records.clear()  # 行動履歴は目的単位でリセット
        self.carryover = ""
        return not self.finished

    # ------------------------------------------------------------------ #
    # system役に渡すコンテキストを組み立てる
    # ------------------------------------------------------------------ #
    def build_context(self, related_memories: list[str] | None = None) -> str:
        parts = [f"[最終ゴール]\n{self.goal}"]

        total = len(self.objectives)
        obj = self.current_objective or "（すべての目的が完了）"
        parts.append(f"[今の目的]（{min(self.index + 1, total)}/{total}）\n{obj}")

        if related_memories:
            parts.append(
                "[関連記憶]\n" + "\n".join(f"- {m}" for m in related_memories)
            )

        lines = []
        if self.carryover:
            lines.append(f"（それ以前の要約）{self.carryover}")
        for i, r in enumerate(self.records, 1):
            lines.append(f"{i}. {r.action} → 結果: {r.result}")
        if lines:
            parts.append("[これまでの行動と結果]\n" + "\n".join(lines))

        # 直近の結果を「現在の環境状態」として強調（planが必ず考慮するように）
        if self.records:
            last = self.records[-1]
            failed = is_failure_result(last.result)
            state = "❌ 直前の手は失敗" if failed else "✓ 直前の手は成功"
            parts.append(
                f"[現在の環境状態（最重要・必ず考慮する）]\n{state}\n"
                f"直前の行動: {last.action}\n直前の結果: {last.result}\n"
                + ("→ 同じ手を繰り返さず、この結果を踏まえて次の手を変えること。"
                   if failed else "→ この結果を前提に次の手を進めること。"))

        # 失敗した行動の一覧（再実行を防ぐ）
        failed_actions = [str(r.action) for r in self.records
                          if is_failure_result(r.result)]
        if failed_actions:
            parts.append("[失敗・無効だった行動（再実行禁止）]\n"
                         + "\n".join(f"- {a}" for a in failed_actions[-6:]))

        return "\n\n".join(parts)


if __name__ == "__main__":
    stm = ShortTermMemory(max_records=2)
    stm.set_goal({
        "type": "goal",
        "goal": "ログを解析しレポートを作成する",
        "objectives": ["ログ形式を調べる", "解析する", "レポート化する"],
        "tags": ["log", "python"],
    })
    print("今の目的:", stm.current_objective)

    stm.add_record({"type": "command", "command": "dir", "reason": "確認"}, "OK")
    stm.add_record({"type": "command", "command": "type a.log", "reason": "中身確認"}, "OK")
    stm.add_record({"type": "code", "language": "python", "code": "...", "reason": "解析"}, "OK")
    # max_records=2 なので最初の1件は要約（carryover）に押し出される

    print("--- build_context ---")
    print(stm.build_context(related_memories=["前回: ログはJSON形式だった（成功）"]))

    print("--- advance ---")
    print("次がある:", stm.advance(), "/ 今の目的:", stm.current_objective)
