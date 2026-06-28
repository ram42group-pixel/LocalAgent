# -*- coding: utf-8 -*-
#providers/adapters.py — 既存の各 control を統一Providerインターフェースに包む
"""
既存の *_control.py（send / send_messages / get_models）はそのまま活かし、
ここで Provider(base) に適合させる薄いアダプタにする。
新しいプロバイダを足すときは、ここにクラスを1つ追加して registry.py に登録するだけ。

各社の差（messages形式・残量ヘッダ・role正規化）は既存controlが吸収済みなので、
アダプタは「呼び出し方を send/recv に合わせる」だけの薄い層になる。
"""
from __future__ import annotations

from providers.base import Provider, Response, LimitError


def _to_response(r) -> Response:
    """各 control の Response(dataclass) を providers.base.Response に変換。"""
    return Response(
        model=getattr(r, "model", ""),
        role=getattr(r, "role", "assistant"),
        content=getattr(r, "content", "") or "",
        done=getattr(r, "done", True),
        raw={"origin": r},
    )


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, default_model: str = ""):
        super().__init__(default_model)
        import ollamas.ollama_control as ctl
        self._ctl = ctl

    def list_models(self) -> list[str]:
        return self._ctl.get_models()

    def _chat(self, messages, model) -> Response:
        return _to_response(self._ctl.send_messages(messages, model=model))


class _OpenAICompatProvider(Provider):
    """
    Groq / Cerebras / OpenRouter 共通の包み方。
    既存 control の send_messages があればそれを、無ければ
    system+user を1本に畳んで send を使う。LimitError は base のものへ正規化。
    """
    _ctl = None  # サブクラスで設定

    def list_models(self) -> list[str]:
        return self._ctl.get_models()

    def _chat(self, messages, model) -> Response:
        try:
            if hasattr(self._ctl, "send_messages"):
                r = self._ctl.send_messages(messages, model=model)
            else:
                # send は単一メッセージ前提なので、system と user を結合して渡す
                text = "\n\n".join(m["content"] for m in messages)
                r = self._ctl.send(text=text, model=model)
        except self._ctl.LimitError as e:
            raise LimitError(getattr(e, "retry_after", None), self.name) from e
        return _to_response(r)


class GroqProvider(_OpenAICompatProvider):
    name = "groq"

    def __init__(self, default_model: str = ""):
        super().__init__(default_model)
        import groq_llm.groq_control as ctl
        self._ctl = ctl


class CerebrasProvider(_OpenAICompatProvider):
    name = "cerebras"

    def __init__(self, default_model: str = ""):
        super().__init__(default_model)
        import cerebras_llm.cerebras_control as ctl
        self._ctl = ctl


class OpenRouterProvider(_OpenAICompatProvider):
    name = "open_router"

    def __init__(self, default_model: str = ""):
        super().__init__(default_model)
        import open_router.open_router_control as ctl
        self._ctl = ctl


class GoogleStudioProvider(Provider):
    name = "google_studio"

    def __init__(self, default_model: str = ""):
        super().__init__(default_model)
        import google_studio.google_studio_control as ctl
        self._ctl = ctl

    def list_models(self) -> list[str]:
        return self._ctl.get_models()

    def _chat(self, messages, model) -> Response:
        # Gemini版 control は単一テキスト＋roleなので、system と user を分けて渡す。
        # system指示は role="system" で送ると control 側が systemInstruction に変換する。
        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        body_parts = [m["content"] for m in messages if m["role"] != "system"]
        try:
            if system_parts and body_parts:
                # systemを効かせるため、まとめて1本に（control側のsystem分離を使う）
                text = "\n\n".join(body_parts)
                r = self._ctl.send(text=text, model=model, role="user")
                # systemは別途先頭に畳む（簡易）：本文の前にsystem指示を付与
                if not r.content:
                    r = self._ctl.send(text="\n\n".join(system_parts + body_parts),
                                       model=model, role="user")
            else:
                text = "\n\n".join(system_parts + body_parts)
                role = "system" if system_parts and not body_parts else "user"
                r = self._ctl.send(text=text, model=model, role=role)
        except self._ctl.LimitError as e:
            raise LimitError(None, self.name) from e
        return _to_response(r)


class MistralProvider(Provider):
    name = "mistral"

    def __init__(self, default_model: str = ""):
        super().__init__(default_model)
        from mistral_llm import mistral_control
        self._ctl = mistral_control
        if not self.default_model:
            self.default_model = mistral_control.DEFAULT_MODEL

    def list_models(self) -> list[str]:
        return self._ctl.get_models()

    def _chat(self, messages, model) -> Response:
        return _to_response(self._ctl.send_messages(messages, model=model))
