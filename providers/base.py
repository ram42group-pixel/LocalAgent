# -*- coding: utf-8 -*-
#providers/base.py — 全LLMプロバイダ共通の型とインターフェース
"""
LLMアクセスを send / recv の2メソッドに統一する土台。

  - Message  : 1メッセージ（role + content）
  - Response : 正規化された応答（全プロバイダ共通の箱）
  - Provider : 抽象基底。各社アダプタはこれを継承し send / recv を実装する
  - LimitError: 429（枠超過）の共通例外

send(messages) でリクエストを投げ、recv() で最後の応答を取り出す。
send は利便のため Response も返す（recv と同じものを返す）ので、
  res = p.send(msgs)        # 送って即受け取る
  res = p.recv()            # 直前の応答を再取得
のどちらでも書ける。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

ROLES = ("user", "system", "assistant")


class LimitError(Exception):
    """429（枠超過）。retry_after 秒待てば回復する見込み。"""
    def __init__(self, retry_after: str | None = None, provider: str = ""):
        super().__init__(f"[{provider}] rate limit (retry-after={retry_after})")
        self.retry_after = retry_after
        self.provider = provider


@dataclass
class Message:
    role: str
    content: str

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class Response:
    """全プロバイダ共通の応答。上位はこれだけを見る。"""
    model: str
    role: str
    content: str
    done: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    raw: dict = field(default_factory=dict)  # プロバイダ固有データの退避先

    def __str__(self) -> str:
        return self.content


def normalize_messages(messages: Iterable[dict | Message]) -> list[dict]:
    """dict でも Message でも受け取れるようにし、role を検証して dict 列にする。"""
    out: list[dict] = []
    for m in messages:
        d = m.to_dict() if isinstance(m, Message) else dict(m)
        if d.get("role") not in ROLES:
            raise ValueError(f"roleは {', '.join(ROLES)} のいずれか: {d.get('role')}")
        if not d.get("content"):
            raise ValueError("contentが空のメッセージがあります")
        out.append(d)
    if not out:
        raise ValueError("messagesが空です")
    return out


class Provider(ABC):
    """
    LLMプロバイダの統一窓口。各社アダプタはこれを継承する。
    name        : プロバイダ識別子
    default_model: send で model 省略時に使うモデル
    """
    name: str = "base"
    default_model: str = ""

    def __init__(self, default_model: str = ""):
        if default_model:
            self.default_model = default_model
        self._last: Response | None = None

    @abstractmethod
    def _chat(self, messages: list[dict], model: str) -> Response:
        """各社の実呼び出し。messages(dict列) → Response。ここだけ実装すればよい。"""
        ...

    @abstractmethod
    def list_models(self) -> list[str]:
        ...

    def send(self, messages: Iterable[dict | Message] | str, model: str = "",
             role: str = "user") -> Response:
        """
        メッセージを送信して応答を得る。
        messages は dict列 / Message列 / 単一の文字列(role指定) のいずれでも可。
        戻り値の Response は recv() でも取り直せる。
        """
        if isinstance(messages, str):
            msgs = normalize_messages([{"role": role, "content": messages}])
        else:
            msgs = normalize_messages(messages)

        model = model or self.default_model
        if not model:
            raise ValueError(f"[{self.name}] modelが未指定（default_modelも空）")

        self._last = self._chat(msgs, model)
        return self._last

    def recv(self) -> Response:
        """直近の send の応答を返す。まだ送っていなければ例外。"""
        if self._last is None:
            raise RuntimeError(f"[{self.name}] まだ send していません")
        return self._last
