# -*- coding: utf-8 -*-
#strategist.py — 攻撃戦略の立案（ReAct的な複数仮説の並行検討＋役割エージェント）
"""
ペンテストを「線形の1手ずつ」から「複数の攻撃仮説を立てて有望な枝を深掘り」へ高度化する。

- propose_hypotheses : 現在の攻撃グラフ状態から、複数の攻撃仮説（次に試すべき経路）を生成
- score_hypotheses   : 各仮説を成功可能性×インパクト×コストで評価し順位付け
- pick_best          : 最有望の仮説を選ぶ
役割エージェント（recon係/exploit係/privesc係）の観点を専門家LLMで使い分ける。
"""
from __future__ import annotations

import json

# 攻撃フェーズ → 担当する役割エージェントの観点
ROLE_AGENTS = {
    "recon":    "偵察担当。攻撃面を広げ、未知のホスト/サービス/エンドポイントを見つける観点で考える。",
    "exploit":  "侵入担当。exploit可能な脆弱性を実際に突いて初期アクセスを取る観点で考える。",
    "privesc":  "権限昇格担当。取得した足場からroot/SYSTEMへ昇格する観点で考える。",
    "lateral":  "横展開担当。取得した認証情報や信頼関係で他ホストへ広げる観点で考える。",
}


def _ask(role_persona: str, user: str, role: str = "plan") -> str:
    """plan系のLLMに役割ペルソナ付きで問う。"""
    from providers import ask
    msgs = [{"role": "system", "content": role_persona},
            {"role": "user", "content": user}]
    return str(ask(role, msgs))


def _graph_text() -> str:
    try:
        import engagement
        with engagement.Engagement() as e:
            return e.prompt_text() or "（インベントリは空）"
    except Exception:
        return "（インベントリ取得不可）"


def propose_hypotheses(objective: str, n: int = 3) -> list[dict]:
    """現在の攻撃グラフを踏まえ、次に試すべき攻撃仮説を複数生成する（ReActのT分岐）。"""
    graph = _graph_text()
    # 役割エージェントの観点を全フェーズ分提示し、多様な仮説を引き出す
    roles_desc = "\n".join(f"- {ph}: {persona}" for ph, persona in ROLE_AGENTS.items())
    persona = ("あなたは熟練ペンテスターの戦略立案者。以下の各役割の視点を切り替えながら、"
               "次に試すべき独立した攻撃仮説を複数挙げる。各仮説は具体的な行動に落とせること。\n"
               f"【役割の視点】\n{roles_desc}")
    prompt = (f"目的: {objective}\n\n現在の攻撃グラフ:\n{graph}\n\n"
              f"次に試すべき攻撃仮説を{n}個、JSON配列で出力せよ。できれば異なるphaseから挙げる。各要素:\n"
              '{"phase":"recon|exploit|privesc|lateral","hypothesis":"仮説の説明",'
              '"first_action":"最初に取る具体的行動(コマンドやツール)","rationale":"なぜ有望か"}\n'
              "JSONのみ。コードフェンス禁止。")
    raw = _ask(persona, prompt)
    hyps = _parse_list(raw)
    return hyps[:n]


def score_hypotheses(objective: str, hypotheses: list[dict]) -> list[dict]:
    """各仮説を 成功可能性/インパクト/コスト で採点し、総合スコア順に並べる。"""
    if not hypotheses:
        return []
    persona = ("あなたはペンテストの戦略評価者。各攻撃仮説を冷静に採点する。"
               "成功可能性(success)・インパクト(impact)・コスト(cost)を各0-10で。")
    prompt = (f"目的: {objective}\n仮説リスト:\n{json.dumps(hypotheses, ensure_ascii=False)}\n\n"
              "各仮説を採点してJSON配列で返せ。各要素:\n"
              '{"index":0,"success":0-10,"impact":0-10,"cost":0-10,"comment":"短評"}\n'
              "JSONのみ。")
    raw = _ask(persona, prompt)
    scores = {s.get("index"): s for s in _parse_list(raw)}
    out = []
    for i, h in enumerate(hypotheses):
        sc = scores.get(i, {})
        success = float(sc.get("success", 5))
        impact = float(sc.get("impact", 5))
        cost = float(sc.get("cost", 5))
        # 総合 = 成功可能性×インパクト ÷ コスト（コスト低いほど良い）
        total = round((success * impact) / max(cost, 1.0), 2)
        out.append({**h, "success": success, "impact": impact, "cost": cost,
                    "total": total, "comment": sc.get("comment", "")})
    out.sort(key=lambda x: -x["total"])
    return out


def pick_best(objective: str, n: int = 3) -> dict | None:
    """仮説生成→採点→最有望を返す（ReActの選択）。"""
    hyps = propose_hypotheses(objective, n=n)
    if not hyps:
        return None
    scored = score_hypotheses(objective, hyps)
    return scored[0] if scored else None


def deliberate(objective: str, n: int = 3) -> dict:
    """完全な戦略熟考：仮説生成→採点→選択を1回で行い、全結果を返す。
    戻り値: {hypotheses: [採点済み全仮説], best: 最有望}"""
    hyps = propose_hypotheses(objective, n=n)
    scored = score_hypotheses(objective, hyps)
    return {"hypotheses": scored, "best": (scored[0] if scored else None)}


def _parse_list(text: str) -> list[dict]:
    """LLM出力からJSON配列を抽出する（配列を正しく扱う）。"""
    import re
    s = text.strip()
    # コードフェンス除去
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    # 最初の [ から対応する ] までを取り出す
    start = s.find("[")
    if start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "[":
                depth += 1
            elif s[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        arr = json.loads(s[start:i + 1])
                        if isinstance(arr, list):
                            return [d for d in arr if isinstance(d, dict)]
                    except Exception:
                        break
    # 配列でなくオブジェクトの場合、配列っぽい値を探す
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, list):
                    return [d for d in v if isinstance(d, dict)]
    except Exception:
        pass
    return []
