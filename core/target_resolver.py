# -*- coding: utf-8 -*-
#core/target_resolver.py — 目標文からターゲット（ホスト/IP/URL）を抽出・解決する
"""
ペネトレーションテストの目標文（例「uuum.jp へのペネトレーションテスト…」）から
実際の対象（ドメイン/IP/URL）を抽出し、DNS解決してIPを得る。

これが無いと、プランナーLLMは対象を知らされず、訓練データ由来の架空IP
（192.168.1.10 など）を幻覚してスキャンし続ける（=偵察が永遠に終わらない）。
抽出した対象は WorldState に事実として登録し、プランナー文脈にも明示注入する。
"""
from __future__ import annotations
import re
import socket

# URL / ドメイン / IPv4 を拾う
_RE_URL = re.compile(r"https?://([^\s/:]+)", re.I)
_RE_IPV4 = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
# ドメイン（example.co.jp / uuum.jp 等）。
# 日本語が直後に続く「uuum.jpへの」のようなケースでも拾えるよう、末尾の\bを使わない。
# 先頭は非英数字境界、本体はラベル.ラベル… で、TLDの直後は非英数字または終端で区切る。
_RE_DOMAIN = re.compile(
    r"(?<![a-z0-9.-])([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)(?![a-z0-9.-])",
    re.I)

# 対象として扱わない一般的な単語の誤抽出を弾く（拡張子・ありがちな語）
_TLD_OK = (".jp", ".com", ".net", ".org", ".io", ".co", ".dev", ".info",
           ".biz", ".gov", ".edu", ".me", ".app", ".ai", ".tv", ".xyz")
_STOPWORDS = {"e.g", "i.e", "vs.", "etc."}


def _looks_like_domain(s: str) -> bool:
    s = s.lower()
    if s in _STOPWORDS:
        return False
    return any(s.endswith(tld) for tld in _TLD_OK)


def extract_target(text: str) -> dict:
    """目標文から対象を1つ抽出する。
    返り値: {kind: url/ip/domain/none, host, url, raw}"""
    if not text:
        return {"kind": "none", "host": "", "url": "", "raw": ""}

    # 1) URL が最優先
    m = _RE_URL.search(text)
    if m:
        host = m.group(1)
        url = m.group(0)
        return {"kind": "url", "host": host, "url": url, "raw": url}

    # 2) IPv4
    m = _RE_IPV4.search(text)
    if m:
        ip = m.group(1)
        # 0-255 の妥当性チェック
        if all(0 <= int(o) <= 255 for o in ip.split(".")):
            return {"kind": "ip", "host": ip, "url": "", "raw": ip}

    # 2.5) localhost（ドットが無く 3) のドメイン検出で拾えないため明示対応）
    m = re.search(r"(?<![a-z0-9.\-])localhost(?![a-z0-9.\-])", text, re.I)
    if m:
        return {"kind": "ip", "host": "127.0.0.1", "url": "", "raw": "localhost"}

    # 3) ドメイン
    for m in _RE_DOMAIN.finditer(text):
        cand = m.group(1)
        if _looks_like_domain(cand):
            return {"kind": "domain", "host": cand, "url": "", "raw": cand}

    return {"kind": "none", "host": "", "url": "", "raw": ""}


def resolve_host(host: str) -> str:
    """ホスト名をIPへ解決する。失敗時は空文字。"""
    if not host:
        return ""
    # 既にIPならそのまま
    if _RE_IPV4.fullmatch(host):
        return host
    try:
        return socket.gethostbyname(host)
    except Exception:
        return ""


def resolve_target(text: str) -> dict:
    """目標文から対象を抽出し、可能ならIPまで解決する。
    返り値: {kind, host, ip, url, resolved(bool), summary}"""
    t = extract_target(text)
    host = t["host"]
    ip = resolve_host(host) if host else ""
    resolved = bool(ip)
    if t["kind"] == "none":
        summary = ""
    else:
        parts = [f"対象ホスト: {host}"]
        if ip and ip != host:
            parts.append(f"解決IP: {ip}")
        elif host and not ip:
            parts.append("（DNS解決できず。名前のまま、またはユーザー確認が必要）")
        if t["url"]:
            parts.append(f"URL: {t['url']}")
        summary = " ／ ".join(parts)
    return {"kind": t["kind"], "host": host, "ip": ip, "url": t["url"],
            "resolved": resolved, "summary": summary}
