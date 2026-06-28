# -*- coding: utf-8 -*-
#open_router_control.py — ollama_control と同じ形のインターフェース（OpenRouter版）
from dataclasses import dataclass
from datetime import datetime

from openai import OpenAI, RateLimitError, APIStatusError

from get_env import env_controler as env

ROLES = ("user", "system", "assistant")
BASE_URL = "https://openrouter.ai/api/v1"


class LimitError(Exception):
    """429（枠超過）。retry_after 秒待てば回復する。"""
    def __init__(self, retry_after: str | None = None):
        super().__init__(f"rate limit (retry-after={retry_after})")
        self.retry_after = retry_after


@dataclass
class Response:
    model: str
    created_at: datetime
    role: str
    content: str
    done: bool

    def __str__(self) -> str:
        return self.content


def _key() -> str:
    import api_keys
    key = api_keys.get_key("open_router")
    if not key:
        raise ValueError("キー未設定: OPEN_ROUTER_API_KEY")
    return key


def _client() -> OpenAI:
    return OpenAI(api_key=_key(), base_url=BASE_URL)


def get_models() -> list[str]:
    return [m.id for m in _client().models.list().data]


def get_free_models() -> list[str]:
    """無料モデル（:free 付き）だけを返す。"""
    return [m for m in get_models() if m.endswith(":free")]


def send(text: str, model: str = "", role: str = "user") -> Response:
    if not text:
        raise ValueError("text が空です")
    if role not in ROLES:
        raise ValueError(f"roleは {', '.join(ROLES)} のいずれかである必要があります")

    if not model:  # 無料モデルを自動選択
        free = get_free_models()
        model = free[0] if free else "openrouter/auto"

    try:
        res = _client().chat.completions.create(
            model=model,
            messages=[{"role": role, "content": text}],
            extra_headers={"HTTP-Referer": "http://localhost", "X-Title": "local-agent"},
        )
    except RateLimitError as e:
        raise LimitError(e.response.headers.get("retry-after")) from e

    choice = res.choices[0]
    return Response(
        model=res.model,
        created_at=datetime.fromtimestamp(res.created),
        role=choice.message.role,
        content=choice.message.content or "",
        done=True,
    )


if __name__ == "__main__":
    free = get_free_models()
    print("無料モデル数:", len(free), "/ 例:", free[:3])

    response = send(text="Pythonについて一言で説明して")
    print("モデル :", response.model)
    print("日時   :", response.created_at)
    print("ロール :", response.role)
    print("本文   :", response.content)
