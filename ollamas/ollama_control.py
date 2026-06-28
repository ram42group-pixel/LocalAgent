# -*- coding: utf-8 -*-
from dataclasses import dataclass
from datetime import datetime
import os
import json as _json

# ollama Python ライブラリは一切使わない。すべて requests による
# REST API 直接呼び出しに統一（ユーザー環境で確実に動く方法）。
# requests が無い環境のためだけに urllib フォールバックを残す。

ROLES = ("user", "system", "assistant")


def _base_url() -> str:
    """ollamaの接続先URL。OLLAMA_HOST を尊重、既定は 127.0.0.1:11434。
    0.0.0.0 / :: は『全インターフェースで待ち受け』のサーバ側指定であり、
    クライアント接続先には使えないため 127.0.0.1 / [::1] に読み替える。"""
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return "http://127.0.0.1:11434"
    scheme = ""
    if host.startswith("http://") or host.startswith("https://"):
        scheme, host = host.split("://", 1)
        host = host.rstrip("/")
    # ポート分離（IPv6 [::]:11434 形式にも配慮）
    port = ""
    h = host
    if h.startswith("[") and "]" in h:           # [::]:port
        addr, _, rest = h.partition("]")
        h = addr.lstrip("[")
        if rest.startswith(":"):
            port = rest[1:]
    elif h.count(":") == 1:                       # host:port
        h, port = h.split(":", 1)
    # 接続不能なワイルドカードアドレスをループバックへ読み替え
    if h in ("0.0.0.0", "::", "[::]", "*", ""):
        h = "127.0.0.1"
    if not port:
        port = "11434"
    return f"{scheme or 'http'}://{h}:{port}"


def _http_post(path: str, payload: dict, timeout: int = 300) -> dict:
    """ollama REST API を直接叩く。ユーザー環境で動作確認済みの requests を使用。
    プロキシ環境(HTTP_PROXY等)でも localhost に直結するため、プロキシを無効化する。
    requests が無い場合のみ urllib にフォールバック。"""
    url = _base_url().rstrip("/") + path
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        # trust_env=False で環境のプロキシ/証明書設定を無視し、localhostへ直結。
        # （アプリ起動時にHTTP_PROXYが効いていると 127.0.0.1 もプロキシ経由になり失敗する）
        sess = requests.Session()
        sess.trust_env = False
        r = sess.post(url, json=payload, timeout=timeout,
                      proxies={"http": None, "https": None})
        r.raise_for_status()
        return r.json()
    # requests 未インストール時のみ urllib（こちらもプロキシ回避）
    import urllib.request
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _http_get(path: str, timeout: int = 10) -> dict:
    """ollama REST API(GET)。ユーザー環境で動く requests を使用。
    プロキシ環境でも localhost に直結するため、プロキシを無効化する。"""
    url = _base_url().rstrip("/") + path
    try:
        import requests
    except ImportError:
        requests = None
    if requests is not None:
        sess = requests.Session()
        sess.trust_env = False
        r = sess.get(url, timeout=timeout, proxies={"http": None, "https": None})
        r.raise_for_status()
        return r.json()
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def _ensure_up() -> None:
    """ollamaに触る前に、未起動なら自動起動を試みる（失敗しても例外は投げない）。"""
    try:
        import ollamas.ollama_server as _srv
        _srv.ensure_ollama()
    except Exception:
        pass


@dataclass
class Response:
    model: str
    created_at: datetime
    role: str
    content: str
    done: bool

    def __str__(self) -> str:
        return self.content

    @classmethod
    def from_ollama(cls, data):
        # ollama クライアントのバージョン差を吸収（新: オブジェクト / 旧: dict）。
        def g(obj, key, default=""):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)
        msg = g(data, "message", {})
        created = g(data, "created_at", "") or ""
        try:
            created_at = (datetime.fromisoformat(created.replace("Z", "+00:00"))
                          if created else datetime.now())
        except Exception:
            created_at = datetime.now()
        return cls(
            model=g(data, "model", "") or "",
            created_at=created_at,
            role=g(msg, "role", "assistant") or "assistant",
            content=g(msg, "content", "") or "",
            done=bool(g(data, "done", True)),
        )


def _do_chat(model: str, messages: list) -> dict:
    """ollamaへチャット要求。requests による REST /api/chat 直接呼び出しのみ。
    戻り値は dict（Response.from_ollama が吸収）。"""
    return _http_post("/api/chat",
                      {"model": model, "messages": messages, "stream": False})


def send(text: str, model: str, role: str = "user") -> Response:
    _ensure_up()
    if not text or not model:
        raise ValueError("text or model が空です")

    if role not in ROLES:
        raise ValueError(
            f"roleは {', '.join(ROLES)} のいずれかである必要があります"
        )

    res = _do_chat(model, [{"role": role, "content": text}])
    return Response.from_ollama(res)


def send_messages(messages: list[dict], model: str) -> Response:
    """
    会話履歴（messagesリスト）ごと送る。send() の複数メッセージ版。
    system + user の2枚送りや、短期記憶を含むコンテキストを渡すときはこちら。
    例: [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
    """
    _ensure_up()
    if not messages or not model:
        raise ValueError("messages or model が空です")

    for m in messages:
        if m.get("role") not in ROLES:
            raise ValueError(
                f"roleは {', '.join(ROLES)} のいずれかである必要があります"
            )
        if not m.get("content"):
            raise ValueError("contentが空のメッセージがあります")

    res = _do_chat(model, messages)
    return Response.from_ollama(res)


def get_models() -> list[str]:
    _ensure_up()
    # requests による REST /api/tags のみ。レスポンス形式の差は吸収する。
    try:
        data = _http_get("/api/tags")
    except Exception as e_http:
        raise RuntimeError(
            f"ollamaに接続できません（ollama serveの起動とOLLAMA_HOSTを確認）: "
            f"{e_http}") from e_http
    out = []
    models = data.get("models", []) if isinstance(data, dict) else []
    for m in models:
        name = m.get("model") or m.get("name") if isinstance(m, dict) else None
        if name:
            out.append(name)
    return out


if __name__ == "__main__":
    models = get_models()

    response = send(
        text="Pythonについて一言で説明して",
        model=models[0]
    )

    print("モデル :", response.model)
    print("日時   :", response.created_at)
    print("ロール :", response.role)
    print("完了   :", response.done)
    print("本文   :", response.content)
