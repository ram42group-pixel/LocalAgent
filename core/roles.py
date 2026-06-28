# -*- coding: utf-8 -*-
#core/roles.py — マルチエージェント役割とモデルルーティングの対応
"""
将来のマルチエージェント化（役割ごとに別モデル）の土台。

アーキテクチャ役割 → providers の routing role の対応表。
各役割は providers.ROLE_ROUTES のキーに対応し、役割マップ画面(/map)から
個別にモデルを差し替えられる。例:
  Planner       → "plan"      → Gemini
  Coder         → "coder"     → Qwen
  Critic        → "judge"     → GPT-OSS
  SecurityExpert→ "security"  → 専用モデル

ask(role, msgs) はこの role 文字列で候補モデル列を引くので、
役割クラスが正しい role を使えば、モデル切替は設定だけで完結する。
"""
from __future__ import annotations

# アーキテクチャ役割 → ルーティングrole
ROLE_MAP = {
    "Planner": "plan",
    "Researcher": "researcher",
    "Executor": "plan",        # 実行自体はLLM不要。計画役と同居でよい
    "Critic": "judge",
    "MemoryManager": "summary",
    "Coder": "coder",
    "SecurityExpert": "security",
    "Narrator": "narrator",
    "Explainer": "explainer",
}


def routing_role(arch_role: str) -> str:
    """アーキテクチャ役割名から providers の routing role を返す。"""
    return ROLE_MAP.get(arch_role, "plan")


def ensure_roles_registered() -> None:
    """マルチエージェント用の役割が ROLE_ROUTES に無ければ追加する。
    既存の plan 候補を流用した安全なデフォルトを入れる（起動時に1回呼ぶ）。"""
    try:
        import providers.registry as reg
    except Exception:
        return
    defaults = {
        # 調査役: 速度と推論のバランス
        "researcher": [("groq", "llama-3.3-70b-versatile"),
                       ("cerebras", "gpt-oss-120b"),
                       ("ollama", "qwen3-coder:30b")],
        # コーダー: コード生成に強いモデル
        "coder": [("cerebras", "gpt-oss-120b"),
                  ("groq", "llama-3.3-70b-versatile"),
                  ("ollama", "qwen3-coder:30b")],
        # セキュリティ専門: ローカル優先（ポリシー非依存で攻撃可）
        "security": [("ollama", "qwen3-coder:30b"),
                     ("cerebras", "gpt-oss-120b"),
                     ("groq", "llama-3.3-70b-versatile")],
    }
    for role, routes in defaults.items():
        if role not in reg.ROLE_ROUTES:
            reg.ROLE_ROUTES[role] = [tuple(t) for t in routes]
