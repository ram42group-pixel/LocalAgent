# -*- coding: utf-8 -*-
#engine/scraper.py — search-engines-scraper ラッパー（Bing/Google/DuckDuckGo等を横断）
"""
pip install git+https://github.com/tasos-py/Search-Engines-Scraper.git
（PyPIには無いのでGitHubから。未インストールなら search() がerrorを返すだけで他は動く）

ライブラリ側が複数エンジン対応なので、SCRAPER_BACKEND で切り替えられる。
"""
from __future__ import annotations

from engine.base import SearchEngine, SearchResult

SCRAPER_BACKEND = "bing"   # bing / google / duckduckgo / yahoo など


def _backend():
    import search_engines as se
    table = {
        "bing": se.Bing, "google": se.Google, "duckduckgo": se.Duckduckgo,
        "yahoo": se.Yahoo, "mojeek": getattr(se, "Mojeek", None),
    }
    cls = table.get(SCRAPER_BACKEND)
    if cls is None:
        raise RuntimeError(f"未対応バックエンド: {SCRAPER_BACKEND}")
    return cls()


class ScraperEngine(SearchEngine):
    name = "scraper"

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        try:
            eng = _backend()
        except ImportError as e:
            raise RuntimeError(
                "search-engines-scraper 未インストール: "
                "pip install git+https://github.com/tasos-py/Search-Engines-Scraper.git"
            ) from e
        eng.disable_console()                      # 進捗printを止める
        res = eng.search(query, pages=1)           # 1ページで十分（≒10件）
        out = []
        for r in res.results()[:limit]:
            out.append(SearchResult(
                title=r.get("title", "") or "",
                url=r.get("link", "") or "",
                snippet=(r.get("text", "") or "")[:300],
            ))
        return out
