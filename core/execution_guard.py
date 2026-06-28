# -*- coding: utf-8 -*-
#core/execution_guard.py — 実行直前のターゲット照合ガード（Phase4）
"""
Executor の直前に挟む最終ゲート。コマンド/URL/引数に含まれる
IP・ホスト・ドメイン・URL をすべて抽出し、Target Context の許可対象と照合する。
1つでも許可外のホストが含まれていれば、コマンドを実行せず Reject する。

自動修正はしない（要件8）。理由を返して、正しいターゲットで出し直させる。

許可例: nikto http://192.168.1.10
拒否例: nikto http://example.com / curl https://google.com /
        sqlmap https://testphp.vulnweb.com
"""
from __future__ import annotations
import re

from core import target_manager

# URL（スキーム付き）からホストを取る
_RE_URL = re.compile(r"\bhttps?://([^\s/:'\"]+)", re.I)
# 素のIPv4（CIDR含む）
_RE_IPV4 = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?:/\d{1,2})?\b")
# 素のドメイン（日本語隣接でも拾えるよう境界をゆるめる）
_RE_DOMAIN = re.compile(
    r"(?<![a-z0-9.@/-])([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)(?![a-z0-9/-])",
    re.I)

# ドメイン誤検出を弾く: ツール名・ファイル拡張子・よくある非ホスト語
_TLD_OK = (".jp", ".com", ".net", ".org", ".io", ".co", ".dev", ".info",
           ".biz", ".gov", ".edu", ".me", ".app", ".ai", ".tv", ".xyz",
           ".uk", ".de", ".fr", ".cn", ".ru", ".kr", ".tw", ".local")
# コマンド内に出るがホストではないドメイン風トークン（パッケージ/ファイル等）
_NON_HOST = set()   # 無条件許可するホストは無し。localhostもターゲット照合に従う
                    # （ターゲットがlocalhostのときのみ target_manager 側で許可される）

# ツール導入・ワードリスト/エクスプロイト取得に使う既知インフラは、
# 攻撃対象でなくてもアクセスを許可する（git clone/pip/wget 等を妨げない）。
# これらは「攻撃の対象」ではなく「攻撃の道具を取りに行く先」。
_INFRA_HOSTS = {
    "github.com", "raw.githubusercontent.com", "githubusercontent.com",
    "gitlab.com", "bitbucket.org", "codeload.github.com",
    "pypi.org", "files.pythonhosted.org", "pythonhosted.org",
    "registry.npmjs.org", "npmjs.com", "npmjs.org",
    "crates.io", "static.crates.io",
    "exploit-db.com", "www.exploit-db.com",
    "packetstormsecurity.com", "cve.mitre.org", "nvd.nist.gov",
    "archive.ubuntu.com", "security.ubuntu.com", "deb.debian.org",
    "kali.org", "http.kali.org",
}


def _is_infra(host: str) -> bool:
    h = host.lower()
    return any(h == d or h.endswith("." + d) for d in _INFRA_HOSTS)


def _valid_ipv4(s: str) -> bool:
    parts = s.split("/")[0].split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _looks_like_host_domain(s: str) -> bool:
    s = s.lower()
    return any(s.endswith(tld) for tld in _TLD_OK)


def extract_hosts(text: str) -> list[str]:
    """コマンド等から、ターゲットになりうるホスト/IP/ドメインを抽出する。"""
    if not text:
        return []
    hosts = []
    seen = set()

    def add(h):
        h = h.lower().strip().rstrip(".")
        if h and h not in seen:
            seen.add(h)
            hosts.append(h)

    # 1) URL のホスト
    for m in _RE_URL.finditer(text):
        add(m.group(1))
    # 2) 素のIPv4
    for m in _RE_IPV4.finditer(text):
        if _valid_ipv4(m.group(0)):
            add(m.group(1))
    # 3) 素のドメイン（TLDが妥当なものだけ）
    for m in _RE_DOMAIN.finditer(text):
        cand = m.group(1)
        if _looks_like_host_domain(cand):
            add(cand)
    # 4) localhost / ::1（ドットが無く 3) で拾えないため明示的に検出）
    for m in re.finditer(r"(?<![a-z0-9.\-])(localhost|::1)(?![a-z0-9.\-])", text, re.I):
        add(m.group(1))
    # 5) user@host 形式（ssh/scp 等）。@の後のホスト名/IPを対象にする
    for m in re.finditer(r"@([a-z0-9][a-z0-9.\-]*)", text, re.I):
        host = m.group(1)
        if _looks_like_host_domain(host) or host.lower() == "localhost" \
                or _RE_IPV4.match(host):
            add(host)
    return hosts


def _command_text(action: dict) -> str:
    """ホスト照合の対象にする文字列。実際に実行/送信される部分を集める。
    - command/url/name: 直接ホストに作用する
    - args: tool型がrecon/exploit等へ渡す引数（ホスト指定がここに入る）
    対象外:
    - code本文: スクリプト内部の例示まで弾くと過剰なため
    - query: web_searchは外部検索で対象ホストに作用しない（外部サイト言及は正常）"""
    if not isinstance(action, dict):
        return str(action)
    # web_search はターゲットに副作用が無いので照合しない（過剰ブロック防止）
    if action.get("type") == "web_search":
        return ""
    parts = [str(action.get(k, "")) for k in ("command", "url", "name")]
    # tool等の args（dict/list/str）を平坦化して含める
    args = action.get("args")
    if isinstance(args, dict):
        parts.extend(str(v) for v in args.values())
    elif isinstance(args, (list, tuple)):
        parts.extend(str(v) for v in args)
    elif args:
        parts.append(str(args))
    return " ".join(parts)


def check(action: dict, ctx: dict, world=None) -> dict:
    """実行直前の最終照合。許可外ホストが含まれれば Reject。
    Phase4.1: 不変ctx（primary）に加え、World Stateの trusted_targets
    （証拠により昇格したターゲット）も許可対象に含める。
    返り値: {ok, reason, suggestion, offending}"""
    # Target未ロック（対象未指定）なら制限しない
    if not ctx or not ctx.get("target_locked"):
        return {"ok": True, "reason": "", "suggestion": "", "offending": ""}

    text = _command_text(action)
    if not text.strip():
        return {"ok": True, "reason": "", "suggestion": "", "offending": ""}

    # Phase4.2: Root から Evidence Chain で到達可能なターゲット集合（経路検証済み）
    reachable = set()
    if world is not None:
        try:
            reachable = world.reachable_targets()
        except Exception:
            # 旧World State（reachable未実装）なら trusted で代替
            try:
                reachable = world.trusted_target_names()
            except Exception:
                reachable = set()

    hosts = extract_hosts(text)
    for h in hosts:
        if h in _NON_HOST:
            continue
        if _is_infra(h):
            continue        # ツール/ワードリスト取得先（github/pypi等）は対象外で許可
        if target_manager.host_allowed(h, ctx):
            continue
        if h in reachable:
            continue        # Root から証拠経路で到達可能 → 許可
        # primaryでも到達可能でもない → 却下（証拠経路が無い）
        primary = ctx.get("primary_target", "")
        allowed = ", ".join(list(ctx.get("allowed_hosts", ()))
                            + list(ctx.get("allowed_networks", ()))
                            + sorted(reachable))
        # 経路が無い理由を説明（Evidence Chainの観点）
        why = ""
        if world is not None:
            try:
                why = world.chain_explanation(h)
            except Exception:
                why = ""
        return {
            "ok": False,
            "offending": h,
            "reason": (f"許可外のホスト '{h}' を対象にしている"
                       f"（Root からの証拠経路なし）"),
            "suggestion": (f"ターゲットは {primary} と、Rootから証拠経路で到達できる"
                           f"ホストのみ。現在の到達可能対象 [{allowed}] 以外には"
                           f"一切アクセスしないこと。'{h}' を使うには、偵察"
                           "(DNS/リダイレクト/リンク/証明書等)で実際に到達経路を"
                           "観測し、Evidence Chain を確立してから対象にすること。"
                           + (f" 現状: {why}" if why else "")),
        }
    return {"ok": True, "reason": "", "suggestion": "", "offending": ""}
