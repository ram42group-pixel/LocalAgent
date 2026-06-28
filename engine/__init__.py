# -*- coding: utf-8 -*-
#engine/__init__.py — Web検索の公開窓口
"""
    from engine import search
    resp = search("検索語", limit=5)          # 既定エンジン＋フォールバック
    resp = search("検索語", engine="tavily")   # エンジン指定
    text = resp.to_text()                      # LLMへ渡す整形済みテキスト
"""
from engine.base import SearchEngine, SearchResult, SearchResponse
from engine.deep import deep_search, fetch_page_text
from engine.registry import (search, get_engine, engine_names, DEFAULT_ENGINE,
                             engine_status, set_required)

__all__ = ["search", "deep_search", "fetch_page_text", "get_engine", "engine_names", "DEFAULT_ENGINE",
           "engine_status", "set_required",
           "SearchEngine", "SearchResult", "SearchResponse"]
