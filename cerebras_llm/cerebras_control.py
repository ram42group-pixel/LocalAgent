# -*- coding: utf-8 -*-
#cerebras_control.py — ollama_control と同じ形のインターフェース（Cerebras版）
from dataclasses import dataclass
from datetime import datetime

from cerebras.cloud.sdk import Cerebras, RateLimitError, APIStatusError

from get_env import env_controler as env

ROLES = ("user", "system", "assistant")

# 直近の send() で取れたレート制限情報（cerebras_limit.py が読む）
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


def _client() -> Cerebras:
    import api_keys
    key = api_keys.get_key("cerebras")
    if not key:
        raise ValueError("キー未設定: CREBRAS_API_KEY")
    return Cerebras(api_key=key)


def get_models() -> list[str]:
    return [m.id for m in _client().models.list().data]


def send(text: str, model: str = "", role: str = "user") -> Response:
    if not text:
        raise ValueError("text が空です")
    if role not in ROLES:
        raise ValueError(f"roleは {', '.join(ROLES)} のいずれかである必要があります")

    if not model:  # 提供モデルが変わりやすいので一覧から選ぶ（決め打ちは404の元）
        models = get_models()
        if not models:
            raise RuntimeError("利用可能なモデルがありません")
        model = "gpt-oss-120b" if "gpt-oss-120b" in models else models[0]

    try:
        raw = _client().chat.completions.with_raw_response.create(
            model=model,
            messages=[{"role": role, "content": text}],
            max_completion_tokens=1024,  # 推論(reasoning)系が途中で切れないよう確保
        )
    except RateLimitError as e:
        raise LimitError(e.response.headers.get("retry-after")) from e

    # 残量ヘッダを保存（Cerebrasは -day / -minute 付きの名前）
    h = raw.headers
    LAST_LIMITS.update({
        "remaining_requests_day": h.get("x-ratelimit-remaining-requests-day"),
        "remaining_tokens_minute": h.get("x-ratelimit-remaining-tokens-minute"),
    })

    res = raw.parse()
    choice = res.choices[0]
    content = choice.message.content or ""  # 推論系は本文がNoneになり得る

    if not content.strip():
        reasoning = getattr(choice.message, "reasoning", "") or ""
        content = f"(本文なし finish_reason={choice.finish_reason} reasoning末尾: {reasoning[-120:]})"

    return Response(
        model=res.model,
        created_at=datetime.fromtimestamp(res.created),
        role=choice.message.role,
        content=content,
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
