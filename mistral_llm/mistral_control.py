# -*- coding: utf-8 -*-
#mistral_control.py — ollama_control と同じ形のインターフェース（Mistral版）
"""
Mistral対応。2モードをサポート：
  1) 通常のチャット補完（model指定）
  2) Mistral Agents（agent_id指定の conversations.start）
APIキーは複数あり得るので、現在選択中のキー名（既定 MISTRAL_API_KEY）を使う。
キー名・agent_id は registry/UI から set_api_key_name() / set_agent_id() で切替可能。
"""
from dataclasses import dataclass
from datetime import datetime

try:                                  # mistralai のバージョン差を吸収
    from mistralai.client import Mistral
except ImportError:                   # 新しめのSDKは直下に Mistral
    from mistralai import Mistral

from get_env import env_controler as env

ROLES = ("user", "system", "assistant")
DEFAULT_MODEL = "mistral-large-latest"

# 現在の設定（UIから変更可能）
_CONFIG = {
    "api_key_name": "MISTRAL_API_KEY",   # 使う環境変数名（複数キー対応）
    "agent_id": "",                      # 指定があれば Agents API を使う
    "agent_version": 0,
}

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


def set_api_key_name(name: str) -> None:
    """使用するAPIキーの環境変数名を切り替える（複数キー対応）。"""
    if name:
        _CONFIG["api_key_name"] = name
        try:
            import api_keys
            api_keys.set_key_name("mistral", name)
        except Exception:
            pass


def set_agent_id(agent_id: str, version: int = 0) -> None:
    """Mistral Agents を使う場合の agent_id を設定（空にすると通常チャット）。"""
    _CONFIG["agent_id"] = agent_id or ""
    _CONFIG["agent_version"] = version


def get_config() -> dict:
    return dict(_CONFIG)


def _client() -> Mistral:
    import api_keys
    # api_keys側の設定を優先（UI統一管理）。未設定時は従来の _CONFIG を使う
    key = api_keys.get_key("mistral") or env.get_env(_CONFIG["api_key_name"])
    if not key:
        raise ValueError(f"キー未設定: {_CONFIG['api_key_name']}")
    return Mistral(api_key=key)


def get_models() -> list[str]:
    try:
        data = _client().models.list()
        return [m.id for m in getattr(data, "data", [])]
    except Exception:
        # 取得失敗時は代表的なモデルを返す
        return [DEFAULT_MODEL, "mistral-small-latest", "open-mistral-nemo"]


def _extract_content(response) -> tuple[str, str]:
    """Mistralの応答（chat / agents）から (role, text) を取り出す。"""
    # chat.complete 形式
    try:
        choice = response.choices[0]
        return choice.message.role, (choice.message.content or "")
    except Exception:
        pass
    # conversations.start 形式（outputs にメッセージが入る）
    try:
        outs = getattr(response, "outputs", None) or []
        texts = []
        for o in outs:
            c = getattr(o, "content", None)
            if isinstance(c, str):
                texts.append(c)
            elif isinstance(c, list):
                for part in c:
                    t = getattr(part, "text", None) or (part.get("text") if isinstance(part, dict) else None)
                    if t:
                        texts.append(t)
        if texts:
            return "assistant", "\n".join(texts)
    except Exception:
        pass
    return "assistant", str(response)


def send_messages(messages: list[dict], model: str = DEFAULT_MODEL) -> Response:
    """messages（role/content の配列）を送る。agent_id設定時はAgents APIを使う。"""
    if not messages:
        raise ValueError("messages が空です")
    model = model or DEFAULT_MODEL
    cli = _client()
    try:
        if _CONFIG["agent_id"]:
            res = cli.beta.conversations.start(
                agent_id=_CONFIG["agent_id"],
                agent_version=_CONFIG["agent_version"],
                inputs=messages,
            )
        else:
            res = cli.chat.complete(model=model, messages=messages)
    except Exception as e:
        # レート制限らしきものは LimitError に変換
        if "429" in str(e) or "rate" in str(e).lower():
            raise LimitError() from e
        raise

    role, content = _extract_content(res)
    return Response(
        model=getattr(res, "model", model),
        created_at=datetime.now(),
        role=role,
        content=content,
        done=True,
    )


def send(text: str, model: str = DEFAULT_MODEL, role: str = "user") -> Response:
    if not text:
        raise ValueError("text が空です")
    if role not in ROLES:
        raise ValueError(f"roleは {', '.join(ROLES)} のいずれか")
    return send_messages([{"role": role, "content": text}], model=model)


if __name__ == "__main__":
    print("モデル例:", get_models()[:3])
    r = send(text="Pythonについて一言で")
    print("本文:", r.content)
