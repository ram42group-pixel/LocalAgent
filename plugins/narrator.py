# -*- coding: utf-8 -*-
#narrator.py — 面白プラグイン「実況ナレーター」
"""
エージェントの各イベントを、スポーツ実況ふうの一言ナレーションに変える。
Web UI に hype 行として流れる。完全ローカル・依存なし・トークン消費ゼロ。
"""
import random

_LINES = {
    "send":  ["{role}、思考開始ッ！", "ここで{role}にボールが渡るッ", "{role}が動いた！"],
    "try":   ["{provider}/{model} に託すッ…！", "頼れる{model}、出番だ", "{provider}コール！"],
    "recv":  ["返ってきたァ！{ms}msの早業ッ", "{model}、見事な返球！", "ナイス、{ms}msッ"],
    "limit": ["うおっと{provider}が枠切れ！交代だ", "{provider}ダウン、次いけ次！"],
    "error": ["{provider}つまずいた！が、ひるまない", "エラー！しかし攻撃は続く"],
    "fail":  ["万策尽きたか…！", "全員ダウン、ここで一旦仕切り直しッ"],
}


_FLOW = {
    "goal_start": "🎬 ミッション受領！作戦を立てるぞ",
    "goal_done": "🧭 ゴール確定、目的に分解完了！",
    "objective_start": "🎯 目的 {index}/{total} に着手！",
    "fn": "⚙ {fn} 実行中…",
    "action": "▶ 第{turn}手、いくぞ！",
    "exec_result": "✅ 結果が返ってきた！",
    "parse_error": "😵 JSONが崩れてる…修復班、出動！",
    "parse_error_repeat": "🔁 さっきと同じ間違い！落ち着いて、やり方を変えよう",
    "repaired": "🩹 修復成功！ナイスリカバリー",
    "duplicate": "↻ 同じ手はもう打ったぞ！別の作戦でいこう",
    "resumed": "↩ 前回の続きから再開！記憶を引き継いだぞ",
    "action_error": "💥 エラー発生！でも止まらない、立て直すぞ",
    "objective_error": "⚠ 目的でつまずいた…次の目的へ進む",
    "judge": "🧐 達成したか判定中…",
    "skill_learned": "🎓 新しいスキルを習得した！",
    "consolidated": "🧹 記憶を整理した",
    "debate": "💬 plannerとcriticが討論中",
    "critic": "🧐 critic がチェック！",
    "retry_strategy": "🔄 失敗、作戦変更だ",
    "replan": "🗺 状況が変わった、再計画！",
    "tool": "🔧 ツールを使うぞ",
    "step_plan": "📋 手順を立てたぞ！多段階プラン",
    "reflect": "🎓 振り返り完了、教訓を学んだ",
    "loop_detected": "🔁 ループ検知！同じ手はやめて打ち切るぞ",
    "objective_giveup": "⏳ 手数いっぱい！一旦切り上げて次へ",
    "final_report": "🏁 全工程おわり！結果発表ッ",
    "memory_save": "📌 記憶に保存した",
    "run_done": "🎉 全目的クリア！",
}

def narrate(event: str, payload: dict) -> str:
    if event == "flow":
        t = payload.get("type", "")
        if t == "parse_error" and payload.get("repeated"):
            t = "parse_error_repeat"
        tmpl = _FLOW.get(t, "")
        if not tmpl:
            return ""
        try:
            return tmpl.format(**payload)
        except (KeyError, IndexError):
            return tmpl
    tmpl = random.choice(_LINES.get(event, ["{role}…！"]))
    try:
        return tmpl.format(**payload)
    except (KeyError, IndexError):
        return tmpl
