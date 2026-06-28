# -*- coding: utf-8 -*-
#modes.py — システムプロンプトのモード（標準/セキュリティ/偵察）を管理
"""
plan役が読むシステムプロンプトを切り替える。Webから選択できる。
全モードとも JSON契約（command/file/code/assist/web_search）は同一なので、
executor や json_checker は一切変更不要。
"""
from __future__ import annotations

# モード名 → (表示名, prompts/ のファイル名（拡張子なし）, 説明)
MODES = {
    "standard": ("標準", "system", "汎用エージェント"),
    "pentest":  ("セキュリティ(ペネトレーションテスト)", "system_pentest",
                 "情報収集→列挙→スキャン→検証→レポート"),
    "recon":    ("偵察・情報収集(OSINT/Recon)", "system_recon",
                 "受動収集→能動列挙→整理→レポート"),
    "killchain": ("サイバーキルチェーン", "system_killchain",
                  "偵察→武器化→配送→攻撃→インストール→C2→目的実行の7段"),
}

_current = {"mode": "pentest"}


def list_modes() -> list[dict]:
    return [{"id": k, "name": v[0], "desc": v[2],
             "active": k == _current["mode"]} for k, v in MODES.items()]


def get_mode() -> str:
    return _current["mode"]


def set_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"未定義のモード: {mode}")
    _current["mode"] = mode


def system_prompt_role() -> str:
    """現在のモードに対応する prompts/ のファイル名（fast_connect.load_prompt 用）。"""
    return MODES[_current["mode"]][1]
