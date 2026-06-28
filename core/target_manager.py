# -*- coding: utf-8 -*-
#core/target_manager.py — ターゲットの唯一の信頼源(Source of Truth)（Phase4）
"""
ユーザーが指定したターゲットを解析・正規化・ロックし、不変の
Target Context を生成する。run開始時に1度だけ作られ、以降変更されない。

すべてのコンポーネント（Planner/Researcher/Exploration/Strategy/Executor/Critic/
Memory）はこの Context を読み取り専用で参照し、ここに無いホストを対象にできない。

Target Context（不変）例:
{
  "primary_target": "192.168.1.10",
  "allowed_hosts": ["192.168.1.10"],
  "allowed_domains": [],
  "allowed_networks": ["192.168.1.0/24"],
  "target_locked": true
}
"""
from __future__ import annotations
import ipaddress
from types import MappingProxyType

from core import target_resolver


def _network_of(ip: str) -> str:
    """IPから /24 ネットワークを導出（例 192.168.1.10 → 192.168.1.0/24）。"""
    try:
        net = ipaddress.ip_network(ip + "/24", strict=False)
        return str(net)
    except Exception:
        return ""


def build_context(request: str) -> dict:
    """目標文からターゲットを解析し、不変の Target Context を構築する。
    解析できない場合 primary_target は空で target_locked=False（未ロック）。"""
    info = target_resolver.resolve_target(request)
    host = (info.get("host") or "").strip()
    ip = (info.get("ip") or "").strip()
    kind = info.get("kind", "none")

    allowed_hosts = []
    allowed_domains = []
    allowed_networks = []
    primary = ""

    if kind == "ip":
        primary = host
        allowed_hosts.append(host)
        net = _network_of(host)
        if net:
            allowed_networks.append(net)
    elif kind in ("domain", "url"):
        primary = host                      # 主ターゲットはホスト名
        allowed_domains.append(host.lower())
        allowed_hosts.append(host.lower())
        if ip:
            allowed_hosts.append(ip)        # 解決済みIPも許可対象に含める
            net = _network_of(ip)
            if net:
                allowed_networks.append(net)
    # localhost は「ターゲット自体が localhost のとき」だけ許可する。
    # xxx.com が対象なのに localhost をスキャンするのは無関係であり禁止。
    _LOCAL = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    target_is_local = (primary.lower() in _LOCAL) or (ip in _LOCAL)
    if target_is_local:
        if "127.0.0.1" not in allowed_hosts:
            allowed_hosts.append("127.0.0.1")
        if "localhost" not in allowed_hosts:
            allowed_hosts.append("localhost")
        if "localhost" not in allowed_domains:
            allowed_domains.append("localhost")
        if "::1" not in allowed_hosts:
            allowed_hosts.append("::1")

    locked = bool(primary)

    ctx = {
        "primary_target": primary,
        "primary_ip": ip,
        "kind": kind,
        "allowed_hosts": sorted(set(h.lower() for h in allowed_hosts)),
        "allowed_domains": sorted(set(allowed_domains)),
        "allowed_networks": sorted(set(allowed_networks)),
        "url": info.get("url", ""),
        "target_locked": locked,
        "resolved": bool(ip),
    }
    return ctx


def freeze(ctx: dict):
    """Target Context を読み取り専用化（誤った書き換えを防ぐ）。
    ネストしたリストは tuple 化し、全体を MappingProxy で包む。"""
    frozen = {}
    for k, v in ctx.items():
        frozen[k] = tuple(v) if isinstance(v, list) else v
    return MappingProxyType(frozen)


def host_allowed(host: str, ctx: dict) -> bool:
    """与えられたホスト/IP/ドメインが Target Context で許可されているか。"""
    if not host:
        return True            # ホスト指定なし（ローカル操作等）は対象外
    if not ctx or not ctx.get("target_locked"):
        return True            # 未ロックなら制限しない（対象未指定時）
    h = host.lower().strip().rstrip(".")

    # 完全一致（ホスト/ドメイン）
    if h in ctx.get("allowed_hosts", ()):
        return True
    if h in ctx.get("allowed_domains", ()):
        return True
    # サブドメイン許可（例: allowed=example.com なら api.example.com も可）
    for dom in ctx.get("allowed_domains", ()):
        if dom and (h == dom or h.endswith("." + dom)):
            return True
    # ネットワーク帯（CIDR）に入るIPか
    try:
        ip_obj = ipaddress.ip_address(h)
        for net in ctx.get("allowed_networks", ()):
            if ip_obj in ipaddress.ip_network(net, strict=False):
                return True
    except ValueError:
        pass
    return False


def summary(ctx: dict) -> str:
    """プランナー文脈へ注入する1行サマリ。"""
    if not ctx or not ctx.get("target_locked"):
        return ""
    parts = [f"主ターゲット: {ctx['primary_target']}"]
    if ctx.get("primary_ip") and ctx["primary_ip"] != ctx["primary_target"]:
        parts.append(f"IP: {ctx['primary_ip']}")
    allowed = list(ctx.get("allowed_hosts", ())) + list(ctx.get("allowed_networks", ()))
    parts.append("許可対象: " + ", ".join(allowed))
    return " ／ ".join(parts)
