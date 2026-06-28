# -*- coding: utf-8 -*-
#engine/tavily.py — Tavily検索（AI/LLMエージェント専用に最適化された検索API）
"""
Tavily は素のSERPではなく、LLM向けに関連度フィルタ済みのスニペットを返す
「AI専用」検索エンジン。1クエリ=1レスポンスで完結し、agentのWeb検索に最適。
無料枠あり（約1,000検索/月・APIキー必要）。標準ライブラリのみで呼ぶ。

キー: get_env の TAVILY_API_KEY（無ければ環境変数）。未設定なら search() がerrorを返す。
"""
from __future__ import annotations

import json
import os
import urllib.request

from engine.base import SearchEngine, SearchResult

_ENDPOINT = "https://api.tavily.com/search"


def _key() -> str:
    try:
        from get_env import env_controler as env
        k = env.get_env("TAVILY_API_KEY")
        if k:
            return k
    except Exception:
        pass
    return os.environ.get("TAVILY_API_KEY", "")


class TavilyEngine(SearchEngine):
    name = "tavily"

    def _search(self, query: str, limit: int) -> list[SearchResult]:
        key = _key()
        if not key:
            raise RuntimeError("TAVILY_API_KEY 未設定")
        payload = json.dumps({
            "api_key": key,
            "query": query,
            "max_results": limit,
            "search_depth": "basic",
            "include_answer": False,
        }).encode()
        req = urllib.request.Request(
            _ENDPOINT, data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
        out: list[SearchResult] = []
        for item in data.get("results", [])[:limit]:
            out.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=(item.get("content", "") or "")[:300],
            ))
        return out
