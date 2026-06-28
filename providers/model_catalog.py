# -*- coding: utf-8 -*-
#providers/model_catalog.py — プロバイダ別の既知モデル一覧とデフォルト
"""
UIのモデル選択リスト用。APIキーが無くても候補を出せるよう、各プロバイダの
代表的なモデルをハードコードしたフォールバック表を持つ。
list_models()がAPIから取れた場合はそちらを優先し、取れない場合はこの表を使う。
"""
from __future__ import annotations

# プロバイダ → 代表的なモデル（先頭がそのプロバイダの既定）
KNOWN_MODELS = {
    "ollama": [
        "qwen3-coder:30b", "gemma4:12b", "lfm2.5:8b", "llama3.3:70b",
        "qwen2.5:14b", "deepseek-r1:14b",
    ],
    "groq": [
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
        "openai/gpt-oss-120b", "openai/gpt-oss-20b",
        "moonshotai/kimi-k2-instruct", "qwen/qwen3-32b",
    ],
    "cerebras": [
        "llama-3.3-70b", "llama3.1-8b", "qwen-3-32b",
        "qwen-3-235b-a22b-instruct", "gpt-oss-120b",
    ],
    "open_router": [
        "anthropic/claude-3.5-sonnet", "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.3-70b-instruct", "qwen/qwen-2.5-coder-32b-instruct",
        "deepseek/deepseek-chat",
    ],
    "google_studio": [
        "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash",
        "gemini-2.0-flash-lite", "gemini-1.5-flash", "gemini-1.5-pro",
    ],
    "mistral": [
        "mistral-large-latest", "mistral-small-latest",
        "open-mistral-nemo", "codestral-latest", "ministral-8b-latest",
    ],
}

# プロバイダ → 既定モデル（リストの先頭）
DEFAULT_MODEL = {p: (m[0] if m else "") for p, m in KNOWN_MODELS.items()}


def models_for(provider: str, live: list[str] | None = None) -> list[str]:
    """APIから取れたlive一覧と既知一覧をマージして返す（重複除去・既知を優先表示）。"""
    known = KNOWN_MODELS.get(provider, [])
    if not live:
        return list(known)
    # liveにあって既知に無いものを末尾に足す
    merged = list(known)
    for m in live:
        if m not in merged:
            merged.append(m)
    return merged


def default_model(provider: str) -> str:
    return DEFAULT_MODEL.get(provider, "")
