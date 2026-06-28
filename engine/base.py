# -*- coding: utf-8 -*-
#engine/base.py — 検索エンジン共通の型と基底
"""
検索エンジンを差し替え・追加しやすくするための土台。
新しいエンジンは SearchEngine を継承し _search を実装、registry に1行登録するだけ。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""

    def __str__(self) -> str:
        return f"- {self.title}\n  {self.url}\n  {self.snippet}".rstrip()


@dataclass
class SearchResponse:
    """全エンジン共通の検索結果。上位（agent）はこれだけ見る。"""
    engine: str
    query: str
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None

    def to_text(self, limit: int = 5) -> str:
        """LLMへ渡すためのプレーンテキスト整形。"""
        if self.error:
            return f"[{self.engine}] 検索失敗: {self.error}"
        if not self.results:
            return f"[{self.engine}] 「{self.query}」の結果は0件"
        head = f"[{self.engine}] 「{self.query}」の検索結果:"
        body = "\n".join(str(r) for r in self.results[:limit])
        return f"{head}\n{body}"


class SearchEngine(ABC):
    name: str = "base"

    @abstractmethod
    def _search(self, query: str, limit: int) -> list[SearchResult]:
        """各エンジンの実検索。結果リストを返す。例外は search() が捕捉する。"""
        ...

    def search(self, query: str, limit: int = 5) -> SearchResponse:
        if not query or not query.strip():
            return SearchResponse(self.name, query, error="queryが空")
        try:
            results = self._search(query.strip(), limit)
            return SearchResponse(self.name, query, results=results)
        except Exception as e:                       # noqa: BLE001
            return SearchResponse(self.name, query, error=str(e)[:200])
