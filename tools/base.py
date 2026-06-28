# -*- coding: utf-8 -*-
#tools/base.py — ツールの共通インターフェース
"""
ツール = 名前・説明・引数スキーマを持ち、run(args)->str を実装したもの。
LLMには name/description/args を提示し、{"type":"tool","name":...,"args":{...}} で呼ばせる。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Tool(ABC):
    name: str = ""
    description: str = ""
    args: dict = {}     # 引数名 -> 説明（LLMへの提示用）

    @abstractmethod
    def run(self, args: dict) -> str:
        """ツールを実行して結果文字列を返す。"""
        ...

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description, "args": self.args}
