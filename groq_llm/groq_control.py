# -*- coding: utf-8 -*-
#groq_control.py — ollama_control と同じ形のインターフェース（Groq版）
from dataclasses import dataclass
from datetime import datetime

from groq import Groq, RateLimitError, APIStatusError

from get_env import env_controler as env

ROLES = ("user", "system", "assistant")
DEFAULT_MODEL = "llama-3.1-8b-instant"

# 直近の send() で取れたレート制限情報（groq_limit.py が読む）
LAST_LIMITS: dict = {}


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


def _client() -> Groq:
    import api_keys
    key = api_keys.get_key("groq")
    if not key:
        raise ValueError("キー未設定: GROQ_API_KEY1")
    return Groq(api_key=key)


def get_models() -> list[str]:
    return [m.id for m in _client().models.list().data]


def send(text: str, model: str = DEFAULT_MODEL, role: str = "user") -> Response:
    if not text or not model:
        raise ValueError("text or model が空です")
    if role not in ROLES:
        raise ValueError(f"roleは {', '.join(ROLES)} のいずれかである必要があります")

    try:
        raw = _client().chat.completions.with_raw_response.create(
            model=model,
            messages=[{"role": role, "content": text}],
        )
    except RateLimitError as e:
        raise LimitError(e.response.headers.get("retry-after")) from e

    # 残量ヘッダを保存（毎レスポンスに付く）
    h = raw.headers
    LAST_LIMITS.update({
        "remaining_requests_day": h.get("x-ratelimit-remaining-requests"),
        "remaining_tokens_minute": h.get("x-ratelimit-remaining-tokens"),
        "reset_tokens": h.get("x-ratelimit-reset-tokens"),
    })

    res = raw.parse()
    choice = res.choices[0]
    return Response(
        model=res.model,
        created_at=datetime.fromtimestamp(res.created),
        role=choice.message.role,
        content=choice.message.content or "",
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
    print("残量   :", LAST_LIMITS)
