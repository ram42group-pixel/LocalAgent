# -*- coding: utf-8 -*-
#memory/extractor.py — 記憶の要約からエンティティと関係をLLM抽出する
"""
ナレッジグラフ用に、要約文から「人物・組織・能力・業務・事実・指標」などの
エンティティと、それらの関係（所属・関係・影響する指標 等）を抽出する。
抽出役は judge と同じ軽量ルートを使う（providers.ask("judge", ...)）。
失敗時は空を返し、保存処理を壊さない。
"""
from __future__ import annotations

import json
import re

_SYS = (
    "あなたは知識グラフ抽出器です。与えられた要約から、登場する重要な"
    "エンティティ（人物/組織/能力/業務/事実/プロジェクト/指標/役職/技術/概念など）と、"
    "それらの関係を抽出します。\n"
    "次のJSONのみを出力してください（説明やコードフェンス禁止）:\n"
    '{"entities":[{"name":"名前","type":"人物"}],'
    '"relations":[{"source":"A","source_type":"人物","target":"B",'
    '"target_type":"組織","label":"所属"}]}\n'
    "type の例: 人物/組織/能力/業務/事実/プロジェクト/指標/役職/技術/概念。\n"
    "label の例: 所属/関係/影響する指標/必要とされる能力/担当/利用/分類/種類。\n"
    "【重要】Wikipediaのように上位概念でつなぐこと。例えば AES や DES は "
    "『対称鍵暗号』という上位概念(type:概念)に『分類』でつなぐ。"
    "暗号関連なら『暗号アルゴリズム』『暗号』等の共通概念ノードを必ず1つは含め、"
    "個別技術をそこに結びつけて、別々の記憶どうしが共通ハブで繋がるようにする。\n"
    "名前は正規化し表記を統一（同じ対象は必ず同じ表記）。"
    "登場しないものは作らない。最大8エンティティ・8関係まで。"
)

# 名前の正規化（表記ゆれで別ノード化しないように）
_NORMALIZE = {
    "AES": "AES", "aes": "AES", "AES暗号": "AES", "AES暗号化": "AES",
    "DES": "DES", "des": "DES", "DES暗号": "DES", "DES暗号化": "DES",
    "RSA": "RSA", "rsa": "RSA",
    "対称鍵暗号方式": "対称鍵暗号", "対称鍵暗号": "対称鍵暗号", "共通鍵暗号": "対称鍵暗号",
    "暗号アルゴリズム": "暗号アルゴリズム", "暗号化アルゴリズム": "暗号アルゴリズム",
}


def _norm(name: str) -> str:
    n = (name or "").strip()
    return _NORMALIZE.get(n, n)


def extract(goal: str, objective: str, summary: str) -> dict:
    from providers import ask
    text = f"ゴール: {goal}\n目的: {objective}\n要約: {summary}"
    res = ask("judge", [{"role": "system", "content": _SYS},
                        {"role": "user", "content": text}])
    m = re.search(r"\{.*\}", res.content, re.S)
    if not m:
        return {"entities": [], "relations": []}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"entities": [], "relations": []}
    ents = []
    for e in data.get("entities", []):
        if e.get("name"):
            e["name"] = _norm(e["name"])
            ents.append(e)
    rels = []
    for r in data.get("relations", []):
        if r.get("source") and r.get("target"):
            r["source"] = _norm(r["source"]); r["target"] = _norm(r["target"])
            rels.append(r)
    return {"entities": ents[:8], "relations": rels[:8]}
