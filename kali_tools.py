# -*- coding: utf-8 -*-
#kali_tools.py — Kali上の利用可能ツールを apt list --installed から取得・キャッシュする
"""
SSH接続中のKaliで `apt list --installed` を実行してパッケージ一覧を取得。
- 1日1回だけ取得（キャッシュ。再接続や同日再実行では再取得しない）
- GUIツール（X11/デスクトップ依存）は除外
- ユーザがUIで「使ってほしいツール」を指定したら、それを優先リストとして扱う
プロンプトには「利用可能なKaliツール」と「優先して使うツール」を渡す。
"""
from __future__ import annotations

import json
import os
import re
import time

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kali_tools.json")
_TTL = 24 * 3600        # 1日（秒）

# GUI/デスクトップ依存で除外したいパッケージ名のパターン
_GUI_PATTERNS = re.compile(
    r"(^|[-_])(gui|gtk|qt[0-9]?|x11|xorg|wayland|desktop|gnome|kde|kde-|plasma|"
    r"libreoffice|firefox|chromium|burpsuite|zaproxy|wireshark-gtk|"
    r"maltego|ghidra|cutter|bless|gimp|vlc|thunar|nautilus|"
    r"xfce|lxde|mate-|cinnamon|icedtea|metasploit-framework-gui)", re.I)
# 明示的にGUIな既知ツール名
_GUI_NAMES = {
    "wireshark", "burpsuite", "zaproxy", "maltego", "ghidra", "cutter",
    "armitage", "bettercap-ui", "firefox-esr", "chromium", "vlc",
    "ettercap-graphical", "ophcrack", "feroxbuster-gui",
}

# 既知の「CLIセキュリティツール」ホワイトリスト寄りの判定補助（任意）
_KNOWN_CLI = {
    "nmap", "masscan", "rustscan", "dnsrecon", "dnsenum", "fierce", "theharvester",
    "amass", "sublist3r", "whois", "dnsutils", "fping", "netdiscover", "arp-scan",
    "recon-ng", "spiderfoot", "gobuster", "ffuf", "dirb", "feroxbuster", "nikto",
    "whatweb", "wpscan", "wafw00f", "nuclei", "hydra", "medusa", "patator", "john",
    "hashcat", "crackmapexec", "cewl", "crunch", "enum4linux", "enum4linux-ng",
    "smbclient", "smbmap", "rpcclient", "ldap-utils", "impacket-scripts", "responder",
    "bloodhound", "kerbrute", "sqlmap", "commix", "xsser", "metasploit-framework",
    "exploitdb", "aircrack-ng", "tshark", "tcpdump", "hashid", "exiftool", "binwalk",
    "steghide", "curl", "wget", "netcat-traditional", "socat", "proxychains4",
}

_state = {"tools": [], "fetched_at": 0, "preferred": []}


def _load():
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        _state.update(tools=d.get("tools", []),
                      fetched_at=d.get("fetched_at", 0),
                      preferred=d.get("preferred", []))
    except Exception:
        pass


def _save():
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"tools": _state["tools"], "fetched_at": _state["fetched_at"],
                       "preferred": _state["preferred"]}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_load()


def _is_gui(name: str) -> bool:
    if name in _GUI_NAMES:
        return True
    return bool(_GUI_PATTERNS.search(name))


def _parse_apt_list(raw: str) -> list[str]:
    """`apt list --installed` の出力からパッケージ名を抜き出し、GUIを除外。"""
    names = []
    for line in raw.splitlines():
        # 形式: name/branch version arch [installed...]
        m = re.match(r"^([a-z0-9][a-z0-9.+-]+)/", line)
        if not m:
            continue
        name = m.group(1)
        if _is_gui(name):
            continue
        names.append(name)
    return sorted(set(names))


def needs_refresh() -> bool:
    return (time.time() - _state["fetched_at"]) > _TTL or not _state["tools"]


def refresh(force: bool = False) -> dict:
    """SSH接続中なら apt list --installed を取得（1日1回。forceで即時）。"""
    import ssh_session
    if not ssh_session.is_connected():
        return {"ok": False, "reason": "SSH未接続", "count": len(_state["tools"])}
    if not force and not needs_refresh():
        return {"ok": True, "cached": True, "count": len(_state["tools"]),
                "fetched_at": _state["fetched_at"]}
    raw = ssh_session.run("apt list --installed 2>/dev/null", timeout=120)
    if raw.startswith("エラー:"):
        return {"ok": False, "reason": raw, "count": len(_state["tools"])}
    tools = _parse_apt_list(raw)
    if tools:
        _state.update(tools=tools, fetched_at=time.time())
        _save()
    return {"ok": True, "cached": False, "count": len(tools),
            "fetched_at": _state["fetched_at"]}


def maybe_refresh() -> None:
    """エージェント実行時に呼ぶ。接続中かつ1日経過していれば自動取得。"""
    try:
        if needs_refresh():
            refresh()
    except Exception:
        pass


def available_tools() -> list[str]:
    return list(_state["tools"])


def set_preferred(names: list[str]) -> None:
    """UIで指定された「使ってほしいツール」を保存。"""
    _state["preferred"] = [n.strip() for n in names if n.strip()]
    _save()


def get_preferred() -> list[str]:
    return list(_state["preferred"])


def prompt_text() -> str:
    """プロンプトに注入する、利用可能ツール＋優先ツールの説明文。"""
    if not _state["tools"]:
        return ""
    pref = _state["preferred"]
    lines = []
    if pref:
        lines.append("【優先して使うツール（ユーザ指定）】" + ", ".join(pref))
    # セキュリティ系として有用なものを優先的に列挙（多すぎを防ぐ）
    sec = [t for t in _state["tools"] if t in _KNOWN_CLI]
    others = [t for t in _state["tools"] if t not in _KNOWN_CLI]
    shown = sec[:60] + others[:40]
    lines.append(f"【Kaliで利用可能なツール（GUI除外, {len(_state['tools'])}個中抜粋）】"
                 + ", ".join(shown))
    lines.append("※ここに無いツールは未導入。必要なら apt-get install -y で導入してから使う。")
    return "\n" + "\n".join(lines)


def status() -> dict:
    return {"count": len(_state["tools"]), "fetched_at": _state["fetched_at"],
            "preferred": _state["preferred"],
            "age_hours": round((time.time() - _state["fetched_at"]) / 3600, 1)
            if _state["fetched_at"] else None}
