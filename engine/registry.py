# -*- coding: utf-8 -*-
#engine/registry.py — 検索エンジンの登録と選択を1か所に集約
"""
エンジンの追加 = ここの _ENGINES に1行足すだけ。
  search(query, engine=None) で検索。engine省略時は DEFAULT_ENGINE。
  フォールバック順 FALLBACK で、失敗/0件なら次のエンジンへ自動で移る。
"""
from __future__ import annotations

from engine.base import SearchResponse
from engine.duckduckgo import DuckDuckGoEngine
from engine.bing import BingEngine
from engine.tavily import TavilyEngine
from engine.scraper import ScraperEngine

# 名前 → エンジンクラス（新エンジンはここに登録）
_ENGINES = {
    "duckduckgo": DuckDuckGoEngine,
    "bing": BingEngine,
    "tavily": TavilyEngine,
    "scraper": ScraperEngine,
}

DEFAULT_ENGINE = "duckduckgo"          # キー不要で動くものを既定に
FALLBACK = ["duckduckgo", "bing", "scraper", "tavily"]    # 任意エンジンを試す順

# 必須/任意の設定（チェック=必須）。
#   必須(True) : 検索のたびに必ず照会し、結果を統合する
#   任意(False): 必須が全滅したときだけフォールバックとして使う
REQUIRED: dict[str, bool] = {
    "duckduckgo": True,    # キー不要なので既定で必須
    "bing": False,         # キー不要のフォールバック（DDGが403等で全滅した時）
    "tavily": False,       # キー設定後にUIで必須に切替可
    "scraper": False,      # 要 pip install（GitHub）。入れたらUIで必須化可
}

_cache: dict[str, object] = {}


def engine_status() -> list[dict]:
    """UI用: 各エンジンの必須/任意状態。"""
    return [{"name": n, "required": REQUIRED.get(n, False)} for n in _ENGINES]


def set_required(name: str, required: bool) -> None:
    if name not in _ENGINES:
        raise ValueError(f"未登録のengine: {name}")
    REQUIRED[name] = bool(required)


def engine_names() -> list[str]:
    return list(_ENGINES)


def get_engine(name: str):
    if name not in _ENGINES:
        raise ValueError(f"未登録のengine: {name}")
    if name not in _cache:
        _cache[name] = _ENGINES[name]()
    return _cache[name]


def search(query: str, limit: int = 5, engine: str | None = None) -> SearchResponse:
    """
    engine指定あり → そのエンジンのみ。
    指定なし → 必須エンジン全部に照会して結果を統合（URL重複は除去）。
               必須が全滅なら、任意エンジンを FALLBACK 順に試す。
    """
    if engine:
        return get_engine(engine).search(query, limit)

    required = [n for n in _ENGINES if REQUIRED.get(n)]
    merged, errors = [], []
    seen = set()
    for name in required:
        resp = get_engine(name).search(query, limit)
        if resp.error:
            errors.append(f"{name}: {resp.error}")
            continue
        for r in resp.results:
            if r.url not in seen:
                seen.add(r.url)
                merged.append(r)
    if merged:
        label = "+".join(required)
        return SearchResponse(label, query, results=merged[:limit * 2])

    # 必須が全滅 → 任意をフォールバック順に
    for name in FALLBACK:
        if REQUIRED.get(name):
            continue
        resp = get_engine(name).search(query, limit)
        if not resp.error and resp.results:
            return resp
        errors.append(f"{name}: {resp.error or '0件'}")
    return SearchResponse("none", query,
                          error=("; ".join(errors) or "全エンジン失敗")
                          + "（DuckDuckGo/Bingが403等で使えない場合は、.envに "
                            "TAVILY_API_KEY を設定すると安定します）")


if __name__ == "__main__":
    print("登録エンジン:", engine_names())
    print(search("python asyncio とは", limit=3).to_text())
