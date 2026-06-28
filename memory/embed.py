# -*- coding: utf-8 -*-
#memory/embed.py — テキストをベクトル化する（ollama優先、無ければ簡易ベクトル）
"""
ollama の埋め込みモデル(nomic-embed-text等)があればそれを使い、
無ければ文字n-gramのハッシュで簡易ベクトルを作る（依存ゼロのフォールバック）。
どちらも cosine 類似度で比較できる固定長ベクトルを返す。
"""
from __future__ import annotations

import hashlib
import math
import re

EMBED_MODEL = "nomic-embed-text"
_DIM = 256          # 簡易ベクトルの次元
_backend = {"mode": None}   # "ollama" / "hash"（初回に判定）


def _ollama_base_url() -> str:
    """ollamaの接続先。OLLAMA_HOST を尊重、既定 127.0.0.1:11434。"""
    import os
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return "http://127.0.0.1:11434"
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    if ":" not in host:
        host = host + ":11434"
    return "http://" + host


def _http_embeddings(text: str) -> list[float] | None:
    """ollama の埋め込みAPIを requests で直接叩く（ユーザー環境で動く方法）。
    requests が無ければ urllib。失敗時は None。"""
    url = _ollama_base_url().rstrip("/") + "/api/embeddings"
    payload = {"model": EMBED_MODEL, "prompt": text or ""}
    try:
        try:
            import requests
            sess = requests.Session()
            sess.trust_env = False
            r = sess.post(url, json=payload, timeout=30,
                          proxies={"http": None, "https": None})
            r.raise_for_status()
            data = r.json()
        except ImportError:
            import urllib.request, json as _json
            req = urllib.request.Request(
                url, data=_json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=30) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        emb = data.get("embedding") if isinstance(data, dict) else None
        return emb or None
    except Exception:
        return None


def _detect_backend() -> str:
    if _backend["mode"]:
        return _backend["mode"]
    # HTTP直叩き（requests）で埋め込みが取れるか確認。ライブラリ不要。
    if _http_embeddings("test"):
        _backend["mode"] = "ollama"
    else:
        _backend["mode"] = "hash"
    return _backend["mode"]


def _hash_vector(text: str) -> list[float]:
    """文字2-gramをハッシュして次元に割り当てる簡易埋め込み。"""
    vec = [0.0] * _DIM
    text = re.sub(r"\s+", "", (text or "").lower())
    grams = [text[i:i + 2] for i in range(max(len(text) - 1, 1))] or [text]
    for g in grams:
        h = int(hashlib.md5(g.encode()).hexdigest(), 16)
        vec[h % _DIM] += 1.0
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def embed(text: str) -> list[float]:
    if _detect_backend() == "ollama":
        emb = _http_embeddings(text)
        if emb:
            return emb
        _backend["mode"] = "hash"   # 途中で落ちたらハッシュへ切替
    return _hash_vector(text)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def backend_name() -> str:
    return _detect_backend()
