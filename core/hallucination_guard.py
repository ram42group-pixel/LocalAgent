# -*- coding: utf-8 -*-
#core/hallucination_guard.py — 事実に反する行動を防ぐ層（Phase2）
"""
行動実行前に、その行動が World State の事実と矛盾しないか検証する。

禁止例: 事実=Apache のみ → 行動「Nginx の CVE 調査」 → Reject
許可例: 事実=Apache       → 行動「Apache の CVE 調査」 → OK

判定方針（誤検出を避けるため保守的に）:
- 行動テキストに、既知でないサービス名が「調査/探索対象」として現れ、
  かつ同種カテゴリで矛盾する既知事実がある場合のみ Reject。
- 事実がまだ何も無い段階（偵察初期）は素通し（何でも調べてよい）。
- サービス以外の一般的な行動（nmap/gobuster等の探索そのもの）は素通し。
"""
from __future__ import annotations
import re

# 監視対象の代表的サービス名（この集合内での「すり替え」を検出する）
_KNOWN_SERVICES = {
    "apache", "nginx", "iis", "lighttpd", "tomcat", "jetty",   # webサーバ
    "mysql", "mariadb", "postgresql", "mssql", "oracle", "mongodb", "redis",  # DB
    "openssh", "dropbear",                                       # ssh
    "vsftpd", "proftpd", "pure-ftpd",                            # ftp
    "exim", "postfix", "sendmail",                               # smtp
    "wordpress", "joomla", "drupal",                             # cms
}

# 同一カテゴリ（片方が事実なら、別の同カテゴリ名の調査は矛盾になりうる）
_CATEGORIES = [
    {"apache", "nginx", "iis", "lighttpd", "tomcat", "jetty"},
    {"mysql", "mariadb", "postgresql", "mssql", "oracle", "mongodb", "redis"},
    {"openssh", "dropbear"},
    {"vsftpd", "proftpd", "pure-ftpd"},
    {"exim", "postfix", "sendmail"},
    {"wordpress", "joomla", "drupal"},
]


def _category_of(svc: str):
    for c in _CATEGORIES:
        if svc in c:
            return c
    return None


def _action_text(action: dict) -> str:
    if not isinstance(action, dict):
        return str(action)
    return " ".join(str(action.get(k, "")) for k in
                    ("command", "url", "name", "reason", "query")).lower()


def validate(action: dict, world) -> dict:
    """行動が世界状態の事実と矛盾しないか検証する。
    返り値: {ok: bool, reason: str, suggestion: str}"""
    text = _action_text(action)
    if not text.strip():
        return {"ok": True, "reason": "", "suggestion": ""}

    try:
        known = world.known_service_names() if world else set()
    except Exception:
        known = set()

    # 事実がまだ無ければ偵察初期。何でも調べてよい（素通し）。
    if not known:
        return {"ok": True, "reason": "", "suggestion": ""}

    # 行動が言及しているサービス名を拾う
    mentioned = {s for s in _KNOWN_SERVICES if re.search(rf"\b{re.escape(s)}\b", text)}
    if not mentioned:
        return {"ok": True, "reason": "", "suggestion": ""}

    # 「調査/探索/攻撃」を示す行動か（単なる言及でなく対象化しているか）
    investigative = any(w in text for w in
                        ("cve", "脆弱性", "exploit", "調査", "scan", "スキャン",
                         "探索", "攻撃", "vuln", "search"))
    if not investigative:
        return {"ok": True, "reason": "", "suggestion": ""}

    # 言及サービスのうち、既知でない & 同カテゴリに既知事実があるものを矛盾とみなす
    for svc in mentioned:
        if svc in known:
            continue   # 事実と一致 → OK
        cat = _category_of(svc)
        if cat and (cat & known):
            confirmed = ", ".join(sorted(cat & known))
            return {
                "ok": False,
                "reason": (f"事実は[{confirmed}]だが、行動は未確認の'{svc}'を対象にしている"
                           "（事実に反する推測）"),
                "suggestion": (f"確認済みの{confirmed}を対象に調査すること"
                               f"（例: {confirmed} のCVE/設定/モジュール調査）"),
            }
    return {"ok": True, "reason": "", "suggestion": ""}


# ---- IPアドレス推測の禁止（Phase: target fidelity）----
import re as _re

# コマンド/URL等に現れるIPv4
_RE_IPV4 = _re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:/\d{1,2})?\b")

# 説明・ドキュメント用に予約された例示IP/プライベート帯（LLMが幻覚しやすい代表例）
# ※ここを「安全」とみなすのではなく、ターゲット不一致なら一律ブロックする。


def _valid_ipv4(s: str) -> bool:
    parts = s.split("/")[0].split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def validate_ip(action: dict, target: dict) -> dict:
    """行動に含まれるIPアドレスが、解決済みターゲットIPと一致するか検証する。
    一致しないIP（=推測・幻覚）が含まれていれば一律で却下する。
    どんな状況でもエージェントにIPを推測させない。

    target: {host, ip, kind, resolved} （agent_loop の _TARGET['info']）
    返り値: {ok, reason, suggestion}"""
    if not isinstance(action, dict):
        return {"ok": True, "reason": "", "suggestion": ""}
    if action.get("type") == "web_search":
        return {"ok": True, "reason": "", "suggestion": ""}
    # 実行に効く部分のみ照合（reason等の説明文の言及で誤ブロックしない）
    parts = [str(action.get(k, "")) for k in ("command", "url", "name")]
    _args = action.get("args")
    if isinstance(_args, dict):
        parts.extend(str(v) for v in _args.values())
    elif isinstance(_args, (list, tuple)):
        parts.extend(str(v) for v in _args)
    elif _args:
        parts.append(str(_args))
    text = " ".join(parts)
    if not text.strip():
        return {"ok": True, "reason": "", "suggestion": ""}

    found = [m.group(0) for m in _RE_IPV4.finditer(text) if _valid_ipv4(m.group(0))]
    if not found:
        return {"ok": True, "reason": "", "suggestion": ""}

    target = target or {}
    target_ip = (target.get("ip") or "").strip()
    target_host = (target.get("host") or "").strip()

    # ターゲットのIPと完全一致するものだけ許可。それ以外のIPは推測とみなし却下。
    for ip in found:
        bare = ip.split("/")[0]
        if target_ip and bare == target_ip:
            continue
        # 127.0.0.1 / localhost（練習用ローカルアプリ web_alert 等）は許可
        if bare == "127.0.0.1":
            continue
        # ここに来たIPはターゲットでも localhost でもない＝推測
        if target_ip:
            sugg = (f"IPを推測しないこと。対象は {target_host}（IP {target_ip}）。"
                    f"スキャン・攻撃は必ず {target_ip} に対して行うこと。")
        elif target_host:
            sugg = (f"IPを推測しないこと。対象IPは未解決です。"
                    f"ホスト名 {target_host} をそのまま指定するか、まずDNS解決すること。")
        else:
            sugg = ("IPを推測しないこと。対象が未指定なので、ユーザーに対象ホストを"
                    "確認するか、与えられたホスト名をそのまま使うこと。")
        return {"ok": False,
                "reason": f"推測されたIP {bare} を使おうとしている（ターゲット不一致）",
                "suggestion": sugg}
    return {"ok": True, "reason": "", "suggestion": ""}
