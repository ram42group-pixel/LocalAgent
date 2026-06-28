# -*- coding: utf-8 -*-
#engine/deep.py — 深掘り検索: 結果のtext(要約文)からLLMが重要な2〜3件を選び、実ページを取得
"""
流れ:
  1. engine.search() で結果一覧（title/url/text）を取得
  2. judge役LLMに一覧を渡し「重要な2〜3件の番号」をJSONで選ばせる
  3. 選ばれたURLへ実アクセスし、HTML→本文テキスト化（標準ライブラリのみ）
  4. 「検索結果一覧 ＋ 各ページ本文の抜粋」を1つのテキストにして返す
LLM選定に失敗したら先頭2件で続行（壊れても止まらない）。
"""
from __future__ import annotations

import json
import re
import urllib.request
from html.parser import HTMLParser

from engine.registry import search as _search

PAGE_LIMIT = 3          # 取得する最大ページ数
PAGE_CHARS = 1500       # 1ページから取り込む本文の上限
_UA = "Mozilla/5.0 (compatible; LocalAgent/1.0)"
_SKIP_TAGS = {"script", "style", "noscript", "header", "footer", "nav", "aside"}


class _TextExtract(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.chunks.append(data.strip())


def fetch_page_text(url: str, limit: int = PAGE_CHARS) -> str:
    """URLへ実アクセスし、本文テキストを抽出して返す。失敗は文字列で報告。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(500_000)
        html = raw.decode("utf-8", "replace")
        p = _TextExtract()
        p.feed(html)
        text = re.sub(r"\s+", " ", " ".join(p.chunks))
        return text[:limit] or "(本文を抽出できず)"
    except Exception as e:                          # noqa: BLE001
        return f"(取得失敗: {str(e)[:120]})"


def _pick_by_llm(query: str, results) -> list[int]:
    """judge役LLMがtext(要約文)を読んで重要な2〜3件の番号を選ぶ。失敗時は[]。"""
    from providers import ask                      # 循環import回避のため遅延import
    listing = "\n".join(
        f"{i + 1}. {r.title}\n   {r.snippet}" for i, r in enumerate(results)
    )
    msgs = [
        {"role": "system",
         "content": "あなたは検索結果の選別器です。質問に最も役立つ結果を2〜3件選び、"
                    '{"picks":[番号]} のJSONのみを出力してください。説明文は禁止。'},
        {"role": "user", "content": f"質問: {query}\n\n検索結果:\n{listing}"},
    ]
    try:
        res = ask("judge", msgs)
        m = re.search(r"\{.*\}", res.content, re.S)
        picks = json.loads(m.group(0)).get("picks", [])
        return [int(p) - 1 for p in picks if 1 <= int(p) <= len(results)][:PAGE_LIMIT]
    except Exception:                               # noqa: BLE001
        return []


def deep_search(query: str, limit: int = 5, engine: str | None = None,
                emit=None) -> str:
    """検索→LLM選定→実ページ取得まで行い、LLMに渡せる1テキストを返す。"""
    def _e(**kw):
        if emit:
            emit(kw)

    resp = _search(query, limit=limit, engine=engine)
    if resp.error or not resp.results:
        return resp.to_text()

    picks = _pick_by_llm(query, resp.results)
    if not picks:                                   # LLM不調でも止まらない
        picks = list(range(min(2, len(resp.results))))
    _e(type="deep_pick", picks=[p + 1 for p in picks],
       urls=[resp.results[p].url for p in picks])

    parts = [resp.to_text(limit)]
    for p in picks:
        r = resp.results[p]
        _e(type="deep_fetch", url=r.url)
        body = fetch_page_text(r.url)
        parts.append(f"\n--- 選定{p + 1}: {r.title}\n{r.url}\n{body}")
    return "\n".join(parts)
