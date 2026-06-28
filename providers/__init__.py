# -*- coding: utf-8 -*-
#providers/__init__.py — 統一LLMアクセスの公開窓口
"""
    from providers import ask
    res = ask("plan", [{"role":"system","content":"..."},{"role":"user","content":"..."}])

    from providers import get_provider
    p = get_provider("ollama", model="gemma4:12b")
    res = p.send("こんにちは")   # send
    res = p.recv()               # recv（直前の応答）
"""
from providers.base import Provider, Response, Message, LimitError, ROLES
from providers.registry import (ask, get_provider, ROLE_ROUTES, routes_for,
                                set_primary, current_routes, provider_names)

__all__ = [
    "ask", "get_provider", "ROLE_ROUTES", "routes_for",
    "set_primary", "current_routes", "provider_names",
    "Provider", "Response", "Message", "LimitError", "ROLES",
]
