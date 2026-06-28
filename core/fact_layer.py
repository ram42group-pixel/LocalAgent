# -*- coding: utf-8 -*-
#core/fact_layer.py — 観測から事実を抽出する層（Phase2）
"""
Observation（コマンド結果などの生テキスト）から、構造化された事実(Fact)を
抽出する。推測は混ぜず、テキスト上に実際に現れたものだけを事実とする。

例:
  入力: "Server: Apache/2.4.49 (Unix)\n80/tcp open http"
  出力: [Fact(type=service,name=apache,value=2.4.49,confidence=1.0),
         Fact(type=port,name=80,value=open,confidence=1.0)]

抽出は正規表現ベースで決定論的（LLM不要・高速・幻覚なし）。
LLMによる補助抽出も可能だが、既定では使わない（事実の純度を守るため）。
"""
from __future__ import annotations
import re
from core.models import Fact


# サービス名+バージョン（Apache/2.4.49, nginx 1.18.0, OpenSSH_8.2 など）
_RE_SERVICE_VER = re.compile(
    r"\b(apache|nginx|openssh|mysql|mariadb|postgresql|php|tomcat|iis|"
    r"vsftpd|proftpd|exim|postfix|samba|redis|mongodb|nodejs|node|"
    r"wordpress|joomla|drupal|jenkins|tomcat|jetty|lighttpd)"
    r"[/_ ]v?(\d+\.\d+(?:\.\d+)?)", re.I)

# ポート行（nmap風: "80/tcp open http"）
_RE_PORT = re.compile(r"\b(\d{1,5})/(tcp|udp)\s+(open|closed|filtered)\s*([a-z0-9_.-]+)?", re.I)

# CVE
_RE_CVE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.I)

# HTTPステータス/エンドポイント（gobuster風: "/admin (Status: 200)"）
_RE_ENDPOINT = re.compile(r"(/[\w./-]+)\s*\(?\s*(?:Status|status|code)?[:=]?\s*(\d{3})\)?")

# OS推定（"OS: Linux", "Ubuntu", "Windows Server 2019"）
_RE_OS = re.compile(r"\b(ubuntu|debian|centos|red\s?hat|windows server \d+|windows \d+|"
                    r"freebsd|alpine)\b", re.I)

# 認証情報らしき値（fact化は控えめ・confidence低め）
_RE_CRED = re.compile(r"(?:password|passwd|pwd|secret|token|api[_-]?key)[\s:=]+([^\s\"']{4,})", re.I)

# Server ヘッダ
_RE_SERVER_HDR = re.compile(r"Server:\s*([A-Za-z][\w./-]+)", re.I)


def extract_facts(observation: str, source: str = "") -> list[Fact]:
    """観測テキストから事実を抽出する。見つかったものだけを返す（推測なし）。"""
    if not observation:
        return []
    text = str(observation)
    facts: list[Fact] = []
    seen = set()

    def add(f: Fact):
        k = f.key()
        if k not in seen:
            seen.add(k)
            facts.append(f)

    # サービス+バージョン（最も重要な事実）
    for m in _RE_SERVICE_VER.finditer(text):
        name = m.group(1).lower()
        ver = m.group(2)
        add(Fact(type="service", name=name, value=ver,
                 confidence=1.0, source=source))

    # Serverヘッダ（バージョンが取れなくてもサービス名は事実）
    for m in _RE_SERVER_HDR.finditer(text):
        token = m.group(1)
        sm = re.match(r"([A-Za-z]+)[/_ ]?v?(\d+\.\d+(?:\.\d+)?)?", token)
        if sm:
            nm = sm.group(1).lower()
            ver = sm.group(2) or ""
            add(Fact(type="service", name=nm, value=ver,
                     confidence=1.0 if ver else 0.9, source=source))

    # ポート
    for m in _RE_PORT.finditer(text):
        port, proto, state, svc = m.group(1), m.group(2), m.group(3), m.group(4)
        add(Fact(type="port", name=port, value=state.lower(),
                 confidence=1.0, source=source))
        if svc and state.lower() == "open":
            add(Fact(type="service_hint", name=svc.lower(), value=port,
                     confidence=0.8, source=source))

    # CVE
    for m in _RE_CVE.finditer(text):
        add(Fact(type="cve", name=m.group(0).upper(), value="",
                 confidence=1.0, source=source))

    # エンドポイント（2xx/3xxのみ事実として採用）
    for m in _RE_ENDPOINT.finditer(text):
        path, code = m.group(1), m.group(2)
        if code and code[0] in ("2", "3"):
            add(Fact(type="endpoint", name=path, value=code,
                     confidence=0.9, source=source))

    # OS
    for m in _RE_OS.finditer(text):
        add(Fact(type="os", name=m.group(1).lower(), value="",
                 confidence=0.85, source=source))

    # 認証情報（confidence控えめ。誤検出しやすいため）
    for m in _RE_CRED.finditer(text):
        add(Fact(type="credential", name="secret", value=m.group(1),
                 confidence=0.7, source=source))

    return facts


def facts_summary(facts: list[Fact]) -> str:
    """事実リストを人間/プランナー向けの短い文字列に。"""
    if not facts:
        return ""
    by_type: dict[str, list[str]] = {}
    for f in facts:
        label = f.name + (f"/{f.value}" if f.value else "")
        by_type.setdefault(f.type, []).append(label)
    parts = [f"{t}: {', '.join(v[:6])}" for t, v in by_type.items()]
    return " ／ ".join(parts)
