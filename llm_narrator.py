# -*- coding: utf-8 -*-
#llm_narrator.py — 実在LLMによる実況ナレーター
"""
従来のテンプレート式ナレーター(plugins/narrator)に代わり、
本物の軽量LLMが各イベントを短い実況コメントに変える。

ログ解説(log_explainer)が「状況の説明」なのに対し、
ナレーターは「テンポの良い短い実況」を担う（役割: narrator）。

設計はlog_explainerと同様にバッチ化してトークンを節約する:
- イベントを数件(既定3件)ためて1回、短い実況を生成。
- narrator ロールのモデル（専用画面で選択可能）を使う。
- 失敗しても本処理に影響しない。
"""
from __future__ import annotations
import threading
import time

# 実況に向かない低レベルイベントは除外
_SKIP = {"llm_request", "llm_response", "send", "try", "recv", "parsed",
         "hype", "explain", "narrate", "stats"}

_BATCH_N = 3          # 実況はテンポ重視で少なめにまとめる
_BATCH_SEC = 5.0


class LLMNarrator:
    def __init__(self, publish, enabled: bool = True):
        """publish: 実況テキストを配信する関数 publish(str)。"""
        self._publish = publish
        self.enabled = enabled
        self._buf = []
        self._lock = threading.Lock()
        self._last = time.time()

    def feed(self, event: dict) -> None:
        if not self.enabled or not isinstance(event, dict):
            return
        etype = event.get("type") or event.get("event") or ""
        if etype in _SKIP:
            return
        with self._lock:
            self._buf.append(self._compact(event))
            n = len(self._buf)
            due = (time.time() - self._last) >= _BATCH_SEC
        if n >= _BATCH_N or (n > 0 and due):
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._buf:
                return
            batch = self._buf[:]
            self._buf = []
            self._last = time.time()
        text = self._narrate(batch)
        if text:
            try:
                self._publish(text)
            except Exception:
                pass

    def _compact(self, event: dict) -> str:
        etype = event.get("type") or event.get("event") or "?"
        bits = []
        for k in ("fn", "objective", "action", "result", "command",
                  "error", "done", "next_phase", "turn"):
            v = event.get(k)
            if v in (None, "", [], {}):
                continue
            if isinstance(v, dict):
                v = v.get("command") or v.get("type") or str(v)[:60]
            s = str(v).replace("\n", " ")
            if len(s) > 80:
                s = s[:80] + "…"
            bits.append(f"{k}={s}")
        return f"[{etype}] " + " ".join(bits)

    def _narrate(self, batch: list) -> str:
        try:
            from providers import ask
        except Exception:
            return ""
        joined = "\n".join(batch)
        prompt = (
            "あなたはペネトレーションテスト自律エージェントの実行を、"
            "スポーツ中継のように熱く短く実況するナレーターです。"
            "以下のログ断片を見て、今まさに起きていることを"
            "1文・40字以内のテンポの良い実況コメントにしてください。"
            "事実に基づき、誇張は演出程度に。説明は不要、実況の一言だけ返すこと。\n\n"
            f"ログ断片:\n{joined}"
        )
        try:
            res = ask("narrator", [{"role": "user", "content": prompt}])
            text = (res.content or "").strip().replace("```", "")
            # 1行・短めに整える
            text = text.splitlines()[0] if text else ""
            return text[:120]
        except Exception:
            return ""
