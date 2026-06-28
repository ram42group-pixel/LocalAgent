# -*- coding: utf-8 -*-
#google_studio_control.py — ollama_control と同じ形のインターフェース（Gemini版）
import time
from dataclasses import dataclass
from datetime import datetime

from google import genai
from google.genai import errors

from get_env import env_controler as env

ROLES = ("user", "system", "assistant")
DEFAULT_MODEL = "gemini-2.5-flash"

# 累計トークン消費（google_studio_limit.py が読む。Geminiは残量APIが無いため自前で記録）
USAGE = {"calls": 0, "total_tokens": 0, "last": None}
_client_cache = {"client": None, "key": None}


def _client_get():
    """APIキー名が変わっても追従できるよう遅延生成＋キャッシュ。"""
    import api_keys
    key = api_keys.get_key("google_studio")
    if _client_cache["client"] is None or _client_cache["key"] != key:
        _client_cache["client"] = genai.Client(api_key=key)
        _client_cache["key"] = key
    return _client_cache["client"]
class LimitError(Exception):
    """429 / RESOURCE_EXHAUSTED（枠超過）。"""


@dataclass
class Response:
    model: str
    created_at: datetime
    role: str
    content: str
    done: bool

    def __str__(self) -> str:
        return self.content

def get_models() -> list[str]:
    return [m.name.replace("models/", "") for m in _client_get().models.list()]


def send(text: str, model: str = DEFAULT_MODEL, role: str = "user") -> Response:
    """
    Gemini は messages 形式ではなく contents/parts 形式。role は user/model のみで
    system は systemInstruction に分離する仕様のため、ここで変換する。
    503(UNAVAILABLE=一時混雑) は2秒待ちで最大3回リトライする。
    """
    if not text or not model:
        raise ValueError("text or model が空です")
    if role not in ROLES:
        raise ValueError(f"roleは {', '.join(ROLES)} のいずれかである必要があります")

    client = _client_get()
    kwargs = {"model": model, "contents": text}
    if role == "system":  # systemはsystemInstructionへ（contentsには軽い指示だけ残す）
        kwargs = {
            "model": model,
            "contents": "上記の指示に従って応答してください。",
            "config": {"system_instruction": text},
        }

    last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(**kwargs)
            break
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if code == 429 or "RESOURCE_EXHAUSTED" in str(e):
                raise LimitError("429 / RESOURCE_EXHAUSTED") from e
            if code == 503:  # 一時混雑 → 少し待ってリトライ
                last_err = e
                time.sleep(2)
                continue
            raise
    else:
        raise RuntimeError(f"503が続くため中断: {last_err}")

    # 消費トークンを記録（残量APIが無いぶん自前で見える化）
    usage = resp.usage_metadata
    USAGE["calls"] += 1
    USAGE["last"] = usage
    if usage and usage.total_token_count:
        USAGE["total_tokens"] += usage.total_token_count

    return Response(
        model=model,
        created_at=datetime.now(),  # Geminiは生成時刻を返さないため現在時刻
        role="assistant",           # Geminiの "model" を共通の "assistant" に正規化
        content=(resp.text or "").strip(),
        done=True,
    )


if __name__ == "__main__":
    models = get_models()
    print("モデル数:", len(models), "/ 例:", models[:5])

    response = send(text="Pythonについて一言で説明して")
    print("モデル :", response.model)
    print("日時   :", response.created_at)
    print("ロール :", response.role)
    print("本文   :", response.content)
    print("消費   :", USAGE)

def analyze_image(image_path: str, prompt: str, model: str = DEFAULT_MODEL) -> str:
    """画像＋プロンプトをGeminiに渡して解析結果テキストを返す（マルチモーダル）。
    セキュリティ診断のスクリーンショット読取などに使う。"""
    import os
    if not os.path.exists(image_path):
        return f"エラー: 画像が見つからない: {image_path}"
    client = _client_get()
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "png"
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "webp": "image/webp"}.get(ext, "image/png")
        from google.genai import types as _t
        part = _t.Part.from_bytes(data=data, mime_type=mime)
        resp = client.models.generate_content(model=model, contents=[prompt, part])
        return resp.text or "(応答なし)"
    except errors.APIError as e:
        if getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e):
            raise LimitError("429") from e
        return f"エラー: 画像解析失敗: {e}"
    except Exception as e:
        return f"エラー: 画像解析失敗: {e}"

