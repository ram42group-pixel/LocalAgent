# -*- coding: utf-8 -*-
#core/evidence_engine.py — 観測から証拠付きで新ターゲットを抽出（Phase4.1）
"""
偵察で得た実際の観測結果（コマンド出力等）から、新しいターゲット候補を
「どの証拠で見つかったか(source)」「信頼度(confidence)」付きで抽出する。

LLMの推測ではなく、ツール出力やネットワーク観測のみを根拠にする。
ここで抽出されたものは Evidence 由来なので Trusted Target へ昇格できる。

対応する証拠源（要件3/6）と信頼度:
  user input          1.00 （Target Managerが付与）
  DNS / Reverse DNS   1.00
  HTTP Redirect       1.00
  HTTP Location Header1.00
  Nmap Service        1.00
  SSL SAN             0.95
  SSL CN              0.95
  robots.txt          0.90
  sitemap.xml         0.90
  HTML Link           0.85
  JavaScript URL      0.80
  WHOIS               0.75
  （LLM Suggestion    0.00：ここでは扱わない。candidateとして別途）
"""
from __future__ import annotations
import re

# ホスト名/ドメイン（日本語隣接でも拾えるよう境界をゆるめる）
_HOST = r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)"
_RE_HOST = re.compile(_HOST, re.I)
_RE_IPV4 = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

# 各証拠源のパターン → (source名, confidence)
_PATTERNS = [
    # HTTP Location / Redirect ヘッダ
    (re.compile(r"(?:Location|location)\s*:\s*https?://" + _HOST, re.I),
     "HTTP Location Header", 1.0),
    (re.compile(r"(?:redirect(?:ed)?\s+to|301|302)\D{0,20}https?://" + _HOST, re.I),
     "HTTP Redirect", 1.0),
    # SSL 証明書 SAN / CN
    (re.compile(r"(?:DNS:|Subject Alternative Name[^\n]*?DNS:)\s*" + _HOST, re.I),
     "SSL SAN", 0.95),
    (re.compile(r"(?:CN\s*=|Common Name[^\n]*?)\s*" + _HOST, re.I),
     "SSL CN", 0.95),
    # robots.txt / sitemap
    (re.compile(r"(?:Sitemap|Disallow|Allow)\s*:\s*https?://" + _HOST, re.I),
     "robots.txt", 0.90),
    (re.compile(r"<loc>\s*https?://" + _HOST, re.I),
     "sitemap.xml", 0.90),
    # HTML link / JS URL
    (re.compile(r"(?:href|src)\s*=\s*[\"']https?://" + _HOST, re.I),
     "HTML Link", 0.85),
    (re.compile(r"(?:fetch|axios\.get|XMLHttpRequest|url\s*[:=])\s*[\"']https?://" + _HOST, re.I),
     "JavaScript URL", 0.80),
    # WHOIS
    (re.compile(r"(?:Name Server|Registrar WHOIS Server)\s*:\s*" + _HOST, re.I),
     "WHOIS", 0.75),
]

# Nmap のサービス検出でホスト名が出るケース（rDNS等）
_RE_NMAP_RDNS = re.compile(r"Nmap scan report for\s+" + _HOST, re.I)
# DNS解決結果
_RE_DNS = re.compile(r"(?:has address|name\s*=|Address:\s*)\s*([a-z0-9.\-]+)", re.I)


def _clean(h: str) -> str:
    return (h or "").lower().strip().rstrip(".").rstrip("/")


def extract_evidence(observation: str, parent: str = "") -> list[dict]:
    """観測テキストから、証拠付きの新ターゲット候補を抽出する。
    返り値: [{target, source, confidence, evidence, parent}, ...]
    LLMの推測は含まない（実観測のみ）。"""
    if not observation:
        return []
    text = str(observation)
    out = []
    seen = set()

    def add(target, source, conf, evidence):
        t = _clean(target)
        if not t or t in seen:
            return
        # 明らかにホストでないもの（拡張子付きファイル名等）を除外
        if "." not in t and not _RE_IPV4.match(t):
            return
        seen.add(t)
        out.append({"target": t, "source": source, "confidence": conf,
                    "evidence": evidence[:160], "parent": _clean(parent)})

    for pat, source, conf in _PATTERNS:
        for m in pat.finditer(text):
            host = m.group(1)
            add(host, source, conf, m.group(0))

    for m in _RE_NMAP_RDNS.finditer(text):
        add(m.group(1), "Nmap Service Detection", 1.0, m.group(0))

    return out


# 証拠源ごとの既定信頼度（Reflectionで調整されうる基準値）
SOURCE_CONFIDENCE = {
    "user": 1.0, "DNS Lookup": 1.0, "Reverse DNS": 1.0,
    "HTTP Redirect": 1.0, "HTTP Location Header": 1.0,
    "Nmap Service Detection": 1.0, "SSL SAN": 0.95, "SSL CN": 0.95,
    "robots.txt": 0.90, "sitemap.xml": 0.90, "HTML Link": 0.85,
    "JavaScript URL": 0.80, "WHOIS": 0.75, "LLM Suggestion": 0.0,
}

# Trusted へ昇格できる最低信頼度（これ未満は candidate 止まり）
TRUST_THRESHOLD = 0.75


# Phase4.2: 証拠源 → Scope Graph のエッジ関係(relation)。
# 「どんな関係で親から子へ到達したか」を表す。
SOURCE_RELATION = {
    "user": "root",
    "DNS Lookup": "dns", "DNS": "dns", "Reverse DNS": "dns",
    "HTTP Redirect": "redirects_to", "HTTP Location Header": "redirects_to",
    "Nmap Service Detection": "resolves_to",
    "SSL SAN": "certificate", "SSL CN": "certificate",
    "robots.txt": "references", "sitemap.xml": "references",
    "HTML Link": "links_to", "JavaScript URL": "links_to",
    "WHOIS": "references", "API Response": "api",
    "SMB Enumeration": "smb", "LDAP Enumeration": "ldap",
    "LLM Suggestion": "llm_guess",
}


def relation_for(source: str) -> str:
    """証拠源から Scope Graph のエッジ関係名を返す。"""
    return SOURCE_RELATION.get(source, "references")
