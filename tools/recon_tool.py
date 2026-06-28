# -*- coding: utf-8 -*-
#tools/recon_tool.py — 発見をインベントリに記録し攻撃経路を導くツール
"""
ペンテスト中の発見（ホスト/サービス/脆弱性/認証情報/発見事項）を
構造化インベントリに記録し、攻撃グラフから次の攻撃経路を導出する。
"""
from __future__ import annotations

import json

from tools.base import Tool


def _eng():
    import engagement
    return engagement.Engagement()


class RecordFindingTool(Tool):
    name = "record"
    description = ("発見をインベントリに記録する（攻撃グラフ用）。kindで種別を指定: "
                  "host/service/vuln/credential/finding。発見の度に記録して状況を構造化する")
    args = {
        "kind": "host / service / vuln / credential / finding",
        "ip": "対象IP（host/service/vuln/credentialで使用）",
        "data": ('種別ごとのデータ(JSON)。例 service: {"port":80,"service":"http","product":"Apache","version":"2.4.49"} / '
                 'vuln: {"cve":"CVE-2021-41773","severity":"HIGH","cvss":7.5,"exploit_available":true,"port":80} / '
                 'credential: {"username":"admin","secret":"pass","kind":"password"} / '
                 'host: {"os":"Linux","hostname":"web01"} / '
                 'finding: {"category":"privesc","title":"sudo設定不備","severity":"high"}'),
    }

    def run(self, args: dict) -> str:
        kind = str(args.get("kind", "")).strip()
        ip = str(args.get("ip", "")).strip()
        data = args.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return "エラー: data はJSONで指定してください"
        if not kind:
            return "エラー: kind を指定してください"
        e = _eng()
        try:
            if kind == "host":
                e.add_host(ip, data.get("hostname", ""), data.get("os", ""),
                           data.get("status", "up"), data.get("notes", ""))
                return f"記録: ホスト {ip}"
            if kind == "service":
                e.add_service(ip, int(data.get("port", 0)), data.get("service", ""),
                              data.get("product", ""), data.get("version", ""),
                              data.get("proto", "tcp"), data.get("banner", ""))
                return f"記録: {ip}:{data.get('port')} {data.get('service','')} {data.get('product','')} {data.get('version','')}"
            if kind == "vuln":
                e.add_vuln(ip, data.get("cve", ""), data.get("title", ""),
                           data.get("severity", ""), float(data.get("cvss", 0) or 0),
                           bool(data.get("exploit_available", False)),
                           bool(data.get("verified", False)),
                           data.get("port"), data.get("notes", ""))
                return f"記録: 脆弱性 {data.get('cve') or data.get('title')} @ {ip}"
            if kind == "credential":
                e.add_credential(data.get("username", ""), data.get("secret", ""),
                                 data.get("kind", "password"), ip,
                                 data.get("realm", ""), data.get("source", ""))
                return f"記録: 認証情報 {data.get('username')} @ {ip or '(対象不明)'}"
            if kind == "finding":
                e.add_finding(data.get("category", "misc"), data.get("title", ""),
                              data.get("detail", ""), data.get("severity", "info"), ip)
                return f"記録: 発見 {data.get('title')}"
            return f"エラー: 未知のkind: {kind}"
        finally:
            e.close()


class AttackStateTool(Tool):
    name = "attack_state"
    description = ("現在の攻撃対象インベントリと、自動導出した攻撃経路（次に狙うべき経路）を返す。"
                  "状況を整理して次の一手を決めたい時に使う")
    args = {"detail": "full なら全インベントリ詳細、paths なら攻撃経路のみ（既定: summary）"}

    def run(self, args: dict) -> str:
        mode = str(args.get("detail", "summary")).strip()
        e = _eng()
        try:
            if mode == "full":
                return json.dumps(e.full_state(), ensure_ascii=False, indent=2)
            if mode == "paths":
                paths = e.attack_paths()
                if not paths:
                    return "攻撃経路はまだ導出されていません（発見を record で記録してください）"
                return "\n".join(f"[{p['type']}] {p['recommend']}" for p in paths)
            txt = e.prompt_text()
            return txt or "インベントリは空です。偵察して record で発見を記録してください。"
        finally:
            e.close()
