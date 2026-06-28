# -*- coding: utf-8 -*-
#experts.py — ツール専門家（Tool Expert）と並列オーケストレーション
"""
各ツールに「専門家LLM（プロバイダ/モデル＋専門プロンプト）」を割り当て、
命令者が自然言語で依頼すると、専門家が引数を決め・ツールを実行し・結果を解釈する。

- 専門家設定は experts.json に永続化（UIから変更可）。
- 複数の専門家を並列実行できる（クラウドのみ並列。ollamaは逐次＝GPU取り合い回避）。
"""
from __future__ import annotations

import concurrent.futures
import json
import os

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "experts.json")

# ツール → 専門家のデフォルト設定
#   provider/model: 使うLLM（""ならそのツールはLLM補助なしで直接実行）
#   persona: 専門家の役割・観点を表す追加プロンプト
_DEFAULTS = {
    "cve_lookup": {
        "provider": "google_studio", "model": "gemini-2.5-flash",
        "persona": "あなたは脆弱性アナリスト。サービス名とバージョンから危険なCVEを特定し、"
                   "深刻度と悪用可能性を見極める。曖昧な場合は最も可能性の高い製品名に正規化する。"},
    "sqlmap": {
        "provider": "groq", "model": "llama-3.3-70b-versatile",
        "persona": "あなたはSQLインジェクション診断の専門家。対象URL/パラメータから"
                   "適切な level/risk と検査範囲を決める。まずは低リスクで確認する。"},
    "metasploit": {
        "provider": "cerebras", "model": "llama-3.3-70b",
        "persona": "あなたはMetasploitオペレーター。目的に合うモジュールを検索・確認する"
                   "コマンドを組み立てる。破壊的なexploit実行は避け、まず検索と情報確認に留める。"},
    "web_scan": {
        "provider": "groq", "model": "llama-3.3-70b-versatile",
        "persona": "あなたはWeb攻撃面の調査員。対象URLの攻撃面（フォーム/入力/ヘッダ/Cookie）を"
                   "漏れなく収集する観点で引数を決める。"},
    "web_inspect": {
        "provider": "google_studio", "model": "gemini-2.5-flash",
        "persona": "あなたはWeb画面の診断官。スクショから管理画面の露出・認証の要否・"
                   "情報漏洩を見抜くための適切な質問を設定する。"},
    "browser": {
        "provider": "groq", "model": "llama-3.3-70b-versatile",
        "persona": "あなたはブラウザ操作の専門家。目的に沿ったページ操作（入力/クリック/取得）を"
                   "正確なセレクタで組み立てる。"},
    "vision": {
        "provider": "google_studio", "model": "gemini-2.5-flash",
        "persona": "あなたは画像診断官。画像から読み取るべき要点を的確な質問にする。"},
    "exploit_run": {
        "provider": "cerebras", "model": "llama-3.3-70b",
        "persona": "あなたは侵入担当。脆弱性に合うMetasploitモジュールと設定を選び、"
                   "まず非破壊のcheckで検証する。LHOST/RPORT等を正確に決める。"},
    "privesc": {
        "provider": "groq", "model": "llama-3.3-70b-versatile",
        "persona": "あなたは権限昇格担当。SUID/sudo/capabilities/cron/カーネルから"
                   "昇格経路を見抜く観点で列挙する。"},
    "lateral": {
        "provider": "groq", "model": "llama-3.3-70b-versatile",
        "persona": "あなたは横展開担当。取得した認証情報をどのホスト/プロトコルへ"
                   "使い回すかを判断する。"},
    "strategize": {
        "provider": "cerebras", "model": "qwen-3-235b-a22b-instruct",
        "persona": "あなたは攻撃戦略立案者。攻撃グラフから複数の攻撃仮説を立て、"
                   "成功可能性とインパクトとコストで評価する。"},
    "report": {
        "provider": "google_studio", "model": "gemini-2.5-pro",
        "persona": "あなたは診断レポート作成者。発見を経営層にも伝わる日本語で、"
                   "深刻度と具体的対策を明確にまとめる。"},
}

_state = {"experts": {}}


def _load():
    base = {k: dict(v) for k, v in _DEFAULTS.items()}
    try:
        with open(_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            base.setdefault(k, {}).update(v)
    except Exception:
        pass
    # 登録済みの全ツールに専門家スロットを自動作成（新ツール追加に自動追従）
    try:
        import tools.registry as tr
        for name in tr.tool_names():
            if name in ("expert", "experts_parallel"):
                continue          # オーケストレーション用ツール自身は対象外
            base.setdefault(name, {"provider": "", "model": "", "persona": ""})
    except Exception:
        pass
    _state["experts"] = base


def refresh() -> None:
    """ツール登録が変わった後に専門家スロットを再構築する。"""
    _load()


def reset_all() -> None:
    """全ツールの専門家を既定設定に戻す（experts.jsonを削除して再構築）。"""
    import os
    try:
        if os.path.exists(_FILE):
            os.remove(_FILE)
    except Exception:
        pass
    _load()


def _save():
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(_state["experts"], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_load()


def get_expert(tool: str) -> dict | None:
    return _state["experts"].get(tool)


def set_expert(tool: str, provider: str = None, model: str = None,
               persona: str = None) -> dict:
    e = _state["experts"].setdefault(tool, {"provider": "", "model": "", "persona": ""})
    if provider is not None:
        e["provider"] = provider
    if model is not None:
        e["model"] = model
    if persona is not None:
        e["persona"] = persona
    _save()
    return e


def all_experts() -> dict:
    return {k: dict(v) for k, v in _state["experts"].items()}


def _is_cloud(provider: str) -> bool:
    return bool(provider) and provider != "ollama"


def _build_args_prompt(tool: str, request: str, persona: str) -> str:
    import tools.registry as tr
    spec = next((s for s in tr.list_specs() if s["name"] == tool), None)
    argdesc = json.dumps(spec.get("args", {}), ensure_ascii=False) if spec else "{}"
    return (f"{persona}\n\n"
            f"ツール「{tool}」を使う。引数スキーマ: {argdesc}\n"
            f"依頼: {request}\n\n"
            "このツールに渡す引数だけをJSONで出力せよ（説明やコードフェンス禁止）。"
            '例: {"url":"http://example.com","level":"2"}')


def ask_expert(tool: str, request: str) -> dict:
    """1人の専門家に依頼：引数生成→ツール実行→結果解釈。
    戻り値: {tool, args, raw, interpretation, provider}"""
    import tools.registry as tr
    exp = get_expert(tool) or {"provider": "", "model": "", "persona": ""}
    provider, model, persona = exp.get("provider", ""), exp.get("model", ""), exp.get("persona", "")
    result = {"tool": tool, "provider": provider, "args": {}, "raw": "",
              "interpretation": ""}

    # 1) 引数生成（専門家LLMがあれば）
    args = {}
    if provider:
        try:
            msgs = [{"role": "system", "content": persona},
                    {"role": "user", "content": _build_args_prompt(tool, request, persona)}]
            resp = _ask_with(provider, model, "plan", msgs)
            text = str(resp)
            # クラウドがポリシー拒否した場合、ローカル(ollama)で再試行
            if _is_refusal(text):
                local = _ask_local("plan", msgs)
                if local is not None:
                    text = local
                    result["interpretation"] = "（クラウドが拒否したためローカルLLMで再実行）"
            args = _extract_json(text) or {}
        except Exception as ex:
            result["interpretation"] = f"引数生成に失敗: {ex}"
    result["args"] = args

    # 2) ツール実行
    raw = tr.run_tool(tool, args)
    result["raw"] = raw

    # 3) 結果を専門家視点で解釈（クラウド専門家がいれば）
    if provider:
        try:
            msgs = [{"role": "system", "content": persona},
                    {"role": "user", "content":
                     f"依頼:{request}\nツール{tool}の実行結果:\n{raw[:2500]}\n\n"
                     "専門家として要点・リスク・次の推奨アクションを簡潔に述べよ。"}]
            result["interpretation"] = str(_ask_with(provider, model, "summary", msgs))
        except Exception as ex:
            result["interpretation"] = f"(解釈省略: {ex})"
    return result


def _ask_with(provider: str, model: str, role: str, messages: list):
    """特定プロバイダ/モデルを直接指定してask（並列スレッドセーフ）。"""
    import providers.registry as reg
    return reg.ask_direct(provider, model, messages, role=role)


_REFUSAL = ("i cannot", "i can't", "cannot assist", "can't assist", "cannot help",
            "unable to assist", "against our", "usage polic", "content polic",
            "i'm sorry", "i am sorry", "お応えできません", "対応できません",
            "できかねます", "支援できません", "お手伝いできません", "ポリシー", "違反")


def _is_refusal(text: str) -> bool:
    """LLM応答がポリシー拒否っぽいか（JSON無し＋拒否語）を判定。"""
    if not text or "{" in text:
        return False
    low = text.lower()
    return any(m in low for m in _REFUSAL)


def _ask_local(role: str, messages: list):
    """ローカル(ollama)で再試行する。応答テキスト or None。"""
    try:
        import providers.registry as reg
        if "ollama" not in reg.provider_names():
            return None
        models = []
        try:
            models = reg.get_provider("ollama").list_models()
        except Exception:
            pass
        res = reg.ask_direct("ollama", models[0] if models else "", messages, role=role)
        return str(res) if not _is_refusal(str(res)) else None
    except Exception:
        return None


def _extract_json(text: str) -> dict | None:
    import json_checker as jc
    data, err = jc._extract_json(text)
    return None if err else data


def run_parallel(tasks: list[dict], max_workers: int = 4) -> list[dict]:
    """複数の専門家依頼を実行。クラウド専門家は並列、ollama専門家は逐次。
    tasks: [{"tool": "...", "request": "..."}]
    """
    cloud, local = [], []
    for t in tasks:
        exp = get_expert(t["tool"]) or {}
        (cloud if _is_cloud(exp.get("provider", "")) else local).append(t)

    results: list[dict] = []
    # クラウドは並列
    if cloud:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(ask_expert, t["tool"], t["request"]): t for t in cloud}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    t = futs[fut]
                    results.append({"tool": t["tool"], "args": {}, "raw": "",
                                    "interpretation": f"エラー: {e}", "provider": ""})
    # ollama（およびLLMなし）は逐次
    for t in local:
        results.append(ask_expert(t["tool"], t["request"]))
    return results
