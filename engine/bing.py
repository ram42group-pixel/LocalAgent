# -*- coding: utf-8 -*-
#engine/bing.py — Bing検索（APIキー不要・HTMLスクレイプ）
"""
Bing の HTML結果ページを叩いて結果を抽出する。DuckDuckGoが403等で使えない時の
キー不要フォールバック。標準ライブラリのみ。
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

from engine.base import SearchEngine, SearchResult

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_ENDPOINT = "https://www.bing.com/search"
# <li class="b_algo"> ... <h2><a href="URL">TITLE</a></h2> ... <p>SNIPPET</p>
_ITEM = re.compile(r'<li class="b_algo">.*?<h2>.*?<a[^>]+href="(.*?)".*?>(.*?)</a>.*?'
                   r'(?:<p[^>]*>(.*?)</p>)?</li>', re.S)


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<.*?>", "", s or "")).strip()


class BingEngine(SearchEngine):
    name = "bing"

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        qs = urllib.parse.urlencode({"q": query, "count": limit})
        req = urllib.request.Request(
            f"{_ENDPOINT}?{qs}",
            headers={"User-Agent": _UA,
                     "Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "ja,en;q=0.7"})
        with urllib.request.urlopen(req, timeout=20) as r:
            page = r.read().decode("utf-8", "replace")
        out: list[SearchResult] = []
        for href, title, snip in _ITEM.findall(page)[:limit]:
            out.append(SearchResult(title=_clean(title), url=href,
                                    snippet=_clean(snip)))
        return out
