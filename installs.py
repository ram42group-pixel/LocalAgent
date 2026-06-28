# -*- coding: utf-8 -*-
#installs.py — エージェントがインストールしたものを記録・表示する
"""
コマンド文字列から pip / apt / npm / winget 等のインストールを検知し、
JSONファイルに追記。CLI でもブラウザでも一覧表示できる。
"""
from __future__ import annotations

import json
import os
import re
import time

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "installed.json")

# (マネージャ名, パッケージ抽出用の正規表現)。サブコマンドが install のものだけ対象
_PATTERNS = [
    ("pip",    re.compile(r"\bpip3?\s+install\s+(.+)", re.I)),
    ("apt",    re.compile(r"\bapt(?:-get)?\s+install\s+(?:-y\s+)?(.+)", re.I)),
    ("npm",    re.compile(r"\bnpm\s+(?:install|i)\s+(?:-g\s+)?(.+)", re.I)),
    ("winget", re.compile(r"\bwinget\s+install\s+(.+)", re.I)),
    ("choco",  re.compile(r"\bchoco\s+install\s+(?:-y\s+)?(.+)", re.I)),
    ("gem",    re.compile(r"\bgem\s+install\s+(.+)", re.I)),
    ("cargo",  re.compile(r"\bcargo\s+install\s+(.+)", re.I)),
    ("brew",   re.compile(r"\bbrew\s+install\s+(.+)", re.I)),
]
# パッケージ名から外すフラグ類
_FLAG = re.compile(r"(^|\s)(-{1,2}[A-Za-z][\w-]*)")


def detect(cmd: str) -> tuple[str, list[str]] | None:
    """コマンドからインストールを検知。(manager, [packages]) か None。"""
    for mgr, pat in _PATTERNS:
        m = pat.search(cmd)
        if not m:
            continue
        rest = _FLAG.sub(" ", m.group(1))            # フラグ除去
        rest = rest.split("&&")[0].split("|")[0].split(">")[0]  # 後続コマンド切り
        pkgs = [p for p in rest.replace(",", " ").split() if p and not p.startswith("-")]
        return mgr, pkgs
    return None


def record(cmd: str, success: bool, where: str = "local") -> dict | None:
    """インストールコマンドなら記録。同じ場所に同じパッケージが既にあれば重複記録しない。"""
    hit = detect(cmd)
    if not hit:
        return None
    mgr, pkgs = hit
    data = load()
    # 既に成功記録があるパッケージは除外（同じライブラリの再インストール防止）
    already = {(e["manager"], p, e.get("where", "local"))
               for e in data if e.get("success")
               for p in e.get("packages", [])}
    new_pkgs = [p for p in pkgs if (mgr, p, where) not in already]
    if not new_pkgs:
        return None     # 全部導入済み → 記録しない（重複防止）
    entry = {
        "manager": mgr, "packages": new_pkgs, "command": cmd, "where": where,
        "success": success, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    data.append(entry)
    with open(_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return entry


def is_installed(manager: str, package: str, where: str = "local") -> bool:
    """そのパッケージが既に導入済みか（LLMの重複インストール判断用）。"""
    for e in load():
        if e.get("success") and e.get("where", "local") == where \
                and e.get("manager") == manager and package in e.get("packages", []):
            return True
    return False


def installed_list() -> list[dict]:
    """導入済みパッケージを (manager, package, where) 単位で平坦化して返す。"""
    out, seen = [], set()
    for e in load():
        for p in e.get("packages", []):
            key = (e["manager"], p, e.get("where", "local"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"manager": e["manager"], "package": p,
                        "where": e.get("where", "local"),
                        "success": e.get("success", True), "ts": e.get("ts", "")})
    return out


def remove(manager: str, package: str, where: str = "local") -> bool:
    """記録から該当パッケージを削除（UIの管理用。実環境のアンインストールはしない）。"""
    data = load(); changed = False
    for e in data:
        if e.get("manager") == manager and e.get("where", "local") == where \
                and package in e.get("packages", []):
            e["packages"] = [p for p in e["packages"] if p != package]
            changed = True
    data = [e for e in data if e.get("packages")]   # 空になった記録は捨てる
    if changed:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return changed


def uninstall_command(manager: str, package: str) -> str:
    """実際にアンインストールするコマンド文字列を返す（UIから実行する場合用）。"""
    table = {"pip": f"pip uninstall -y {package}",
             "apt": f"apt-get remove -y {package}",
             "npm": f"npm uninstall -g {package}",
             "gem": f"gem uninstall {package}",
             "cargo": f"cargo uninstall {package}",
             "brew": f"brew uninstall {package}",
             "choco": f"choco uninstall -y {package}",
             "winget": f"winget uninstall {package}"}
    return table.get(manager, f"# {manager} の削除コマンド不明: {package}")


def load() -> list[dict]:
    if not os.path.exists(_FILE):
        return []
    try:
        with open(_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def summary() -> str:
    """CLI表示用の一覧テキスト。"""
    data = load()
    if not data:
        return "インストール履歴なし"
    lines = ["=== インストール済み ==="]
    for e in data:
        mark = "✓" if e["success"] else "✗"
        lines.append(f"{mark} [{e['manager']}] {', '.join(e['packages'])}  ({e['ts']})")
    return "\n".join(lines)


def clear() -> None:
    if os.path.exists(_FILE):
        os.remove(_FILE)


if __name__ == "__main__":
    for c in ["pip install requests numpy", "apt-get install -y nmap",
              "npm i -g typescript", "echo hello"]:
        print(c, "->", detect(c))
