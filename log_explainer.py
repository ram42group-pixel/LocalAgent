# -*- coding: utf-8 -*-
#log_explainer.py — 逐次ログを専用LLMで噛み砕いて説明する
"""
膨大になりがちな詳細ログを、軽量LLMが数イベントごとにまとめて
「今エージェントが何をして、どうなっているか」を平易な日本語で説明する。

設計:
- イベントをバッファに溜め、一定数(既定6件)たまるか一定秒数経過で1回まとめて要約。
  → 毎イベント呼ばないのでトークン節約。膨大ログを圧縮して可読化。
- 要約役には軽量・安価なロール(summary)を使う。失敗しても本処理に影響しない。
- 出力は explain イベントとしてUIへ流す（専用ペインに表示）。

使い方（web_app側のSSEワーカー内）:
    ex = LogExplainer(publish=lambda text: _publish(run_id, {"event":"explain","payload":{"text":text}}))
    ...各イベントごとに...
    ex.feed(event_dict)
    ...終了時...
    ex.flush()
"""
from __future__ import annotations
import threading
import time

# 説明に値しない低レベルイベント（要約から除外してノイズを減らす）
_SKIP = {"llm_request", "llm_response", "send", "try", "recv", "parsed",
         "hype", "explain", "stats"}

# 1バッチの最大イベント数 / 最大待ち秒数
_BATCH_N = 6
_BATCH_SEC = 8.0


class LogExplainer:
    def __init__(self, publish, enabled: bool = True):
        """publish: 説明テキストを受け取って配信する関数 publish(str)。"""
        self._publish = publish
        self.enabled = enabled
        self._buf = []
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self._prev_summary = ""      # 直前の説明（文脈の連続性のため）

    def feed(self, event: dict) -> None:
        """イベントを1件受け取る。条件を満たせばまとめて説明を生成。"""
        if not self.enabled or not isinstance(event, dict):
            return
        etype = event.get("type") or event.get("event") or ""
        if etype in _SKIP:
            return
        with self._lock:
            self._buf.append(self._compact(event))
            n = len(self._buf)
            due = (time.time() - self._last_flush) >= _BATCH_SEC
        if n >= _BATCH_N or (n > 0 and due):
            self.flush()

    def flush(self) -> None:
        """溜まったイベントを1回の要約にまとめて配信する。"""
        with self._lock:
            if not self._buf:
                return
            batch = self._buf[:]
            self._buf = []
            self._last_flush = time.time()
            prev = self._prev_summary
        text = self._summarize(batch, prev)
        if text:
            with self._lock:
                self._prev_summary = text
            try:
                self._publish(text)
            except Exception:
                pass

    # --- 内部 ---
    def _compact(self, event: dict) -> str:
        """1イベントを短い1行に圧縮（要約LLMへの入力を軽くする）。"""
        etype = event.get("type") or event.get("event") or "?"
        # 主要フィールドだけ拾う
        bits = []
        for k in ("fn", "objective", "action", "result", "reason", "command",
                  "error", "done", "message", "next_phase", "readiness",
                  "summary", "goal", "streak"):
            v = event.get(k)
            if v in (None, "", [], {}):
                continue
            if isinstance(v, dict):
                v = v.get("command") or v.get("type") or v.get("message") or str(v)[:80]
            s = str(v).replace("\n", " ")
            if len(s) > 120:
                s = s[:120] + "…"
            bits.append(f"{k}={s}")
        line = f"[{etype}] " + " ".join(bits)
        return line[:300]

    def _summarize(self, batch: list, prev: str) -> str:
        """軽量LLMでバッチを平易な日本語1〜2文に要約する。失敗時は空。"""
        try:
            from providers import ask
        except Exception:
            return ""
        joined = "\n".join(batch)
        ctx = (f"直前までの説明: {prev}\n\n" if prev else "")
        prompt = (
            "あなたはペネトレーションテスト自律エージェントの実行ログを、"
            "ユーザーに分かりやすく実況解説するアシスタントです。"
            "以下の生ログ断片を読み、今エージェントが何をしていて、"
            "どういう状況か（進展・つまずき・次の狙い）を、専門用語を補いながら"
            "日本語で簡潔に1〜2文で説明してください。"
            "誇張や創作はせず、ログにある事実だけを述べること。"
            "JSONやコードは書かず、説明文だけを返すこと。\n\n"
            f"{ctx}生ログ断片:\n{joined}"
        )
        try:
            res = ask("explainer", [{"role": "user", "content": prompt}])
            text = (res.content or "").strip()
            # 念のため記号やコードフェンスを除去
            text = text.replace("```", "").strip()
            return text[:500]
        except Exception:
            return ""
