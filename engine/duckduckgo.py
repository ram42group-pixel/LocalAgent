# -*- coding: utf-8 -*-
#engine/duckduckgo.py — DuckDuckGo検索（APIキー不要）
"""
DuckDuckGo の HTML版エンドポイント(html.duckduckgo.com)を叩いて結果を抽出する。
APIキー不要・標準ライブラリのみ。外部依存を増やさない方針。
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request

from engine.base import SearchEngine, SearchResult

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_ENDPOINT = "https://html.duckduckgo.com/html/"
_ENDPOINT_LITE = "https://lite.duckduckgo.com/lite/"
# 結果リンクと本文スニペットの素朴な抽出（HTML構造変化に備え緩めに）
_LINK = re.compile(r'<a[^>]+class="result__a"[^>]+href="(.*?)".*?>(.*?)</a>', re.S)
_SNIP = re.compile(r'<a[^>]+class="result__snippet".*?>(.*?)</a>', re.S)
# Lite版用（テーブルレイアウト）
_LITE_LINK = re.compile(r'<a[^>]+class="result-link"[^>]*href="(.*?)"[^>]*>(.*?)</a>', re.S)


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<.*?>", "", s)).strip()


def _unwrap(href: str) -> str:
    # DuckDuckGoのリダイレクト(/l/?uddg=...)から実URLを取り出す
    m = re.search(r"uddg=([^&]+)", href)
    return urllib.parse.unquote(m.group(1)) if m else href


def _fetch(endpoint: str, query: str) -> str:
    data = urllib.parse.urlencode({"q": query}).encode()
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://duckduckgo.com/",
    }
    req = urllib.request.Request(endpoint, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


class DuckDuckGoEngine(SearchEngine):
    name = "duckduckgo"

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        # まず html 版。403等で失敗したら lite 版にフォールバック。
        page = ""
        try:
            page = _fetch(_ENDPOINT, query)
        except Exception:
            page = ""
        links = _LINK.findall(page) if page else []
        snips = _SNIP.findall(page) if page else []
        out: list[SearchResult] = []
        for i, (href, title) in enumerate(links[:limit]):
            snippet = _clean(snips[i]) if i < len(snips) else ""
            out.append(SearchResult(title=_clean(title), url=_unwrap(href), snippet=snippet))
        if out:
            return out
        # フォールバック: lite 版
        try:
            page = _fetch(_ENDPOINT_LITE, query)
        except Exception:
            return out
        for href, title in _LITE_LINK.findall(page)[:limit]:
            out.append(SearchResult(title=_clean(title), url=_unwrap(href), snippet=""))
        return out
