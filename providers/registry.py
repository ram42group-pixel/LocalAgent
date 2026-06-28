# -*- coding: utf-8 -*-
#providers/registry.py — プロバイダ/モデルの差し替えを1か所に集約
"""
プロバイダとモデルの選択をここだけで管理する。差し替えはこのファイルを編集するだけ。

  役割(role) → 試す (provider, model) の並び  ……ROLE_ROUTES
  プロバイダ名 → アダプタ生成                  ……get_provider()
  ask(role, messages) で「役割」を指定するだけで、適切なモデルに送り、
  429/未導入なら次の候補へ自動フォールバックする。

切り替え例:
  ・計画をGeminiにしたい → ROLE_ROUTES["plan"] の先頭を ("google_studio", "gemini-3.5-flash") に
  ・完全オフライン       → 環境変数 AGENT_OFFLINE=1 で ollama だけに絞られる
"""
from __future__ import annotations

import os

from providers.base import Response, LimitError, Message
from providers import adapters

# ------------------------------------------------------------------ #
# プロバイダ名 → アダプタクラス（新規プロバイダはここに1行足すだけ）
# ------------------------------------------------------------------ #
_PROVIDERS = {
    "ollama":        adapters.OllamaProvider,
    "groq":          adapters.GroqProvider,
    "cerebras":      adapters.CerebrasProvider,
    "open_router":   adapters.OpenRouterProvider,
    "google_studio": adapters.GoogleStudioProvider,
    "mistral":       adapters.MistralProvider,
}

# ------------------------------------------------------------------ #
# 役割 → 試す順（先頭が第一候補、以降はフォールバック）
#   ここを書き換えるだけでモデル/プロバイダを差し替えられる。
# ------------------------------------------------------------------ #
ROLE_ROUTES: dict[str, list[tuple[str, str]]] = {
    # 計画者（system.txt → task JSON）。高速クラウドを優先、最後にローカル保険。
    "plan": [
        ("cerebras", "gpt-oss-120b"),
        ("groq", "llama-3.3-70b-versatile"),
        ("ollama", "qwen3-coder:30b"),
    ],
    # 目標設計（goal.txt → goal/objectives/tags）
    "goal": [
        ("cerebras", "gpt-oss-120b"),
        ("groq", "llama-3.3-70b-versatile"),
        ("ollama", "qwen3-coder:30b"),
    ],
    # 要約者・記憶圧縮（assistant.txt → summary）。軽量高速モデル。
    "summary": [
        ("groq", "llama-3.1-8b-instant"),
        ("cerebras", "llama3.1-8b"),
        ("ollama", "gemma4:12b"),
    ],
    # 軽い判定・分類。最速の小型モデルで十分。
    "judge": [
        ("groq", "llama-3.1-8b-instant"),
        ("cerebras", "llama3.1-8b"),
        ("ollama", "lfm2.5:8b"),
    ],
    # 実況ナレーター（イベントを一言で実況）。軽量・高速モデルで十分。
    "narrator": [
        ("groq", "llama-3.1-8b-instant"),
        ("cerebras", "llama3.1-8b"),
        ("ollama", "lfm2.5:8b"),
    ],
    # ログ解説（膨大ログを噛み砕いて説明）。要約系の軽量モデル。
    "explainer": [
        ("groq", "llama-3.1-8b-instant"),
        ("cerebras", "llama3.1-8b"),
        ("ollama", "gemma4:12b"),
    ],
}

# オフラインモード: ollama 以外を候補から外す
OFFLINE = os.environ.get("AGENT_OFFLINE") == "1"

# ルート設定の永続化（モデル差し替えが再起動後も残るように）
_ROUTES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "routes.json")


def _save_routes() -> None:
    try:
        import json as _j
        with open(_ROUTES_FILE, "w", encoding="utf-8") as f:
            _j.dump({r: [list(t) for t in ROLE_ROUTES[r]] for r in ROLE_ROUTES},
                    f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_routes() -> None:
    try:
        import json as _j
        if not os.path.exists(_ROUTES_FILE):
            return
        with open(_ROUTES_FILE, encoding="utf-8") as f:
            saved = _j.load(f)
        for role, routes in saved.items():
            if role in ROLE_ROUTES and routes:
                ROLE_ROUTES[role] = [tuple(t) for t in routes]
    except Exception:
        pass


_load_routes()   # 起動時に保存済みルートを復元

# 生成済みプロバイダのキャッシュ（クライアント再生成を避ける）
_cache: dict[str, object] = {}

# LLM呼び出しを観測するフック（Web UI のSSEログ等が登録する）。
# 引数: event(str), payload(dict)
_OBSERVERS: list = []


def add_observer(fn):
    _OBSERVERS.append(fn)


def _emit(event: str, payload: dict):
    for fn in list(_OBSERVERS):
        try:
            fn(event, payload)
        except Exception:
            pass


def get_provider(name: str, model: str = ""):
    """プロバイダ名からアダプタを取得（キャッシュ付き）。"""
    if name not in _PROVIDERS:
        raise ValueError(f"未登録のprovider: {name}")
    if name not in _cache:
        _cache[name] = _PROVIDERS[name](default_model=model)
    return _cache[name]


def ask_direct(provider: str, model: str, messages, role: str = "summary"):
    """特定の provider/model を直接指定して送る（ROLE_ROUTESを汚さない＝並列安全）。
    provider が空なら通常の role ルーティングにフォールバック。"""
    if not provider:
        return ask(role, messages)
    if OFFLINE and provider != "ollama":
        return ask(role, messages)        # オフライン時はクラウド直指定を無効化
    import time
    t0 = time.time()
    prov = get_provider(provider)
    _emit("try", {"role": role, "provider": provider, "model": model})
    res = prov.send(messages, model=model)
    _emit("recv", {"role": role, "provider": provider, "model": model,
                   "ms": int((time.time() - t0) * 1000), "content": res.content})
    return res


def provider_names() -> list[str]:
    return list(_PROVIDERS)


def current_routes() -> dict[str, list[list[str]]]:
    return {r: [list(t) for t in ROLE_ROUTES[r]] for r in ROLE_ROUTES}


def set_primary(role: str, provider: str, model: str) -> None:
    """指定 (provider, model) をその役割の第一候補にする（ブラウザからの差し替え用）。"""
    if provider not in _PROVIDERS:
        raise ValueError(f"未登録のprovider: {provider}")
    pair = (provider, model)
    rest = [t for t in ROLE_ROUTES.get(role, []) if t != pair]
    ROLE_ROUTES[role] = [pair] + rest
    _save_routes()


def routes_for(role: str) -> list[tuple[str, str]]:
    if role not in ROLE_ROUTES:
        raise ValueError(f"未定義のrole: {role}（{list(ROLE_ROUTES)} のいずれか）")
    routes = ROLE_ROUTES[role]
    if OFFLINE:
        routes = [(p, m) for (p, m) in routes if p == "ollama"]
        if not routes:
            raise RuntimeError(f"OFFLINEだが role={role} に ollama 候補が無い")
    return routes


def set_model(role: str, provider: str, model: str):
    """実行中に役割の第一候補を差し替える（Web UIのモデル変更が呼ぶ）。"""
    routes = ROLE_ROUTES.get(role, [])
    routes = [(provider, model)] + [(p, m) for (p, m) in routes
                                    if not (p == provider and m == model)]
    ROLE_ROUTES[role] = routes
    _save_routes()


def ask(role: str, messages: list[dict | Message] | str) -> Response:
    """
    役割を指定して送信。候補を先頭から試し、LimitError や未導入は次へ送る。
    全候補が落ちたら最後の例外を送出。各段階を _emit で観測可能にする。
    """
    import time
    last_err: Exception | None = None
    # プレビューは system プロンプトを除外（ログに出さない）。user入力のみ。
    if isinstance(messages, str):
        preview = messages
    else:
        parts = []
        for m in messages:
            mrole = m["role"] if isinstance(m, dict) else m.role
            content = m["content"] if isinstance(m, dict) else m.content
            if mrole == "system":
                continue            # システムプロンプトはログに出さない
            parts.append(content)
        preview = " | ".join(parts)
    _emit("send", {"role": role, "prompt_preview": preview[:1200],
                   "routes": routes_for(role)})
    for provider_name, model in routes_for(role):
        t0 = time.time()
        # レート制限/一時障害は少し待てば回復することが多い。各候補で短くリトライ。
        for _try in range(2):
            try:
                provider = get_provider(provider_name)
                _emit("try", {"role": role, "provider": provider_name, "model": model})
                res = provider.send(messages, model=model)
                _emit("recv", {"role": role, "provider": provider_name, "model": model,
                               "ms": int((time.time() - t0) * 1000),
                               "content": res.content})
                return res
            except LimitError as e:
                last_err = e
                _emit("limit", {"role": role, "provider": provider_name,
                                "model": model, "retry": _try})
                if _try == 0:
                    time.sleep(2.0)   # 1度だけ待って再試行、ダメなら次候補へ
                    continue
                break
            except Exception as e:
                last_err = e
                _emit("error", {"role": role, "provider": provider_name,
                                "model": model, "error": str(e)[:300]})
                break   # レート制限以外の即エラーは次候補へ
    _emit("fail", {"role": role, "error": str(last_err)})
    raise RuntimeError(f"role={role}: 全候補が失敗しました（最後: {last_err}）")


if __name__ == "__main__":
    # 登録状況とルートの確認（ネットワーク不要）
    print("登録プロバイダ:", list(_PROVIDERS))
    print("OFFLINE:", OFFLINE)
    for role in ROLE_ROUTES:
        print(f"  {role}: {routes_for(role)}")
