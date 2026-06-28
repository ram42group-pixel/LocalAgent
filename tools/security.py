# -*- coding: utf-8 -*-
#tools/security.py — セキュリティ診断ツール（CVE照合 / Metasploit連携）
"""
- CveLookupTool: サービス名+バージョンから既知の脆弱性を照合。
    searchsploit（Kaliオフライン）と NVD API（オンライン最新）を両方使う。
- MetasploitTool: searchsploit/CVEで見つかったものを msfconsole -x で非対話検証。
SSH接続中はKali上で、未接続ならローカルで実行を試みる。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from tools.base import Tool


def _run_cmd(cmd: str, timeout: int = 120) -> str:
    """SSH接続中ならKaliで、なければローカルで実行。"""
    import ssh_session
    if ssh_session.is_connected():
        return ssh_session.run(cmd, timeout=timeout)
    import shell
    import subprocess
    try:
        proc = subprocess.run(shell.shell_prefix() + [cmd], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              **shell.no_window_kwargs())
        return proc.stdout or proc.stderr or "(出力なし)"
    except FileNotFoundError:
        return "エラー: コマンドが見つかりません"
    except Exception as ex:
        return f"エラー: {ex}"


class CveLookupTool(Tool):
    name = "cve_lookup"
    description = ("サービス名とバージョンから既知の脆弱性(CVE/Exploit)を照合する。"
                  "searchsploit(Kali) と NVD API の両方を使う")
    args = {"service": "サービス/製品名（例: Apache, OpenSSH, vsftpd）",
            "version": "バージョン（例: 2.4.49。任意）"}

    def run(self, args: dict) -> str:
        service = str(args.get("service", "")).strip()
        version = str(args.get("version", "")).strip()
        if not service:
            return "エラー: service を指定してください"
        query = f"{service} {version}".strip()
        out = [f"== 脆弱性照合: {query} =="]

        # 1) searchsploit（Kali/ローカルにあれば。オフラインで速い）
        ss = _run_cmd(f"searchsploit --color=false {query}", timeout=60)
        if ss.startswith("エラー") or "command not found" in ss.lower() or "見つかりません" in ss:
            out.append("[searchsploit] 利用不可（apt-get install exploitdb で導入可）")
        else:
            # 結果行を抜粋（Exploit Title | Path の表）
            lines = [l for l in ss.splitlines()
                     if l.strip() and "----" not in l and "Exploit Title" not in l]
            hits = lines[:15]
            out.append(f"[searchsploit] {len(lines)}件" +
                       ("\n" + "\n".join(hits) if hits else "（該当なし）"))

        # 2) NVD API（オンライン・最新CVE。CVSS付き）
        nvd = self._nvd_lookup(service, version)
        out.append(nvd)
        return "\n\n".join(out)

    def _nvd_lookup(self, service: str, version: str) -> str:
        import os
        import time
        kw = f"{service} {version}".strip()
        url = ("https://services.nvd.nist.gov/rest/json/cves/2.0?keywordSearch="
               + urllib.parse.quote(kw) + "&resultsPerPage=8")
        headers = {"User-Agent": "LocalAgent/1.0 (security scanner)"}
        # NVD APIキー（任意）。.env からも読めるよう get_env を優先。
        api_key = None
        try:
            from get_env import env_controler as _env
            api_key = _env.get_env("NVD_API_KEY")
        except Exception:
            pass
        api_key = api_key or os.environ.get("NVD_API_KEY")
        if api_key:
            headers["apiKey"] = api_key
        last_err = None
        for attempt in range(3):                  # 403/レート制限に備えてリトライ
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
                break
            except Exception as ex:
                last_err = ex
                time.sleep(2 if api_key else 6)    # キー無しは長めに待つ
        else:
            hint = "" if api_key else "（NVD_API_KEYを設定すると安定します）"
            return f"[NVD] 取得失敗{hint}: {last_err}"
        vulns = data.get("vulnerabilities", [])
        if not vulns:
            return "[NVD] 該当CVEなし"
        rows = []
        for v in vulns[:8]:
            cve = v.get("cve", {})
            cid = cve.get("id", "?")
            sev, score = self._severity(cve)
            desc = ""
            for d in cve.get("descriptions", []):
                if d.get("lang") == "en":
                    desc = d.get("value", "")[:90]
                    break
            rows.append(f"  {cid} [{sev} {score}] {desc}")
        return f"[NVD] {len(vulns)}件（CVSS順抜粋）:\n" + "\n".join(rows)

    @staticmethod
    def _severity(cve: dict) -> tuple[str, str]:
        metrics = cve.get("metrics", {})
        for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
            if key in metrics and metrics[key]:
                m = metrics[key][0].get("cvssData", {})
                return (m.get("baseSeverity", "?"), str(m.get("baseScore", "?")))
        return ("?", "?")


class MetasploitTool(Tool):
    name = "metasploit"
    description = ("Metasploitを非対話(-x)で実行する。モジュール検索や、"
                   "見つかったexploitの確認に使う。常駐させず1回で結果を返す")
    args = {"commands": "msfconsole に渡すコマンド（; 区切り。例: search vsftpd; info exploit/...）"}

    def run(self, args: dict) -> str:
        cmds = str(args.get("commands", "")).strip()
        if not cmds:
            return "エラー: commands を指定してください"
        # 必ず exit で終わらせて常駐を防ぐ
        if "exit" not in cmds:
            cmds = cmds.rstrip(";") + "; exit"
        # クォートをエスケープ
        safe = cmds.replace('"', '\\"')
        return _run_cmd(f'msfconsole -q -x "{safe}"', timeout=300)


class McpTool(Tool):
    name = "mcp"
    description = ("登録済みの外部MCPサーバのツールを呼び出す。"
                  "action=list でサーバ/ツール一覧、action=call で実行")
    args = {"action": "list または call",
            "server": "MCPサーバ名（mcp_servers.jsonに登録済みのもの）",
            "tool": "呼び出すツール名（action=call時）",
            "arguments": "ツールに渡す引数の辞書（action=call時）"}

    def run(self, args: dict) -> str:
        from tools import mcp_client
        action = str(args.get("action", "list"))
        if action == "list":
            server = args.get("server")
            if not server:
                servers = mcp_client.list_servers()
                if not servers:
                    return ("登録済みMCPサーバなし。/api/mcp_add で追加できます。"
                            "例: filesystem, github 等")
                return "登録MCPサーバ: " + ", ".join(servers)
            tools = mcp_client.list_remote_tools(server)
            if not tools:
                return f"{server}: ツールが取得できませんでした（起動失敗の可能性）"
            lines = [f"{server} のツール:"]
            for t in tools:
                lines.append(f"  - {t.get('name')}: {t.get('description', '')[:60]}")
            return "\n".join(lines)
        if action == "call":
            server = args.get("server", "")
            tool = args.get("tool", "")
            if not server or not tool:
                return "エラー: server と tool が必要です"
            return mcp_client.call_remote_tool(server, tool, args.get("arguments", {}))
        return f"エラー: action は list か call（指定: {action}）"


class SqlmapTool(Tool):
    name = "sqlmap"
    description = ("sqlmapでSQLインジェクションを検査する（動的Web診断の実地検証）。"
                   "対象URLやリクエストに対し非対話(--batch)で実行する")
    args = {"url": "検査対象URL（例: http://target/page?id=1）",
            "data": "POSTデータ（任意。例: id=1&name=x）",
            "level": "検査の深さ1-5（任意・既定1）",
            "risk": "リスク1-3（任意・既定1）",
            "extra": "追加オプション（任意。例: --dbs, --tables, -D dbname）"}

    def run(self, args: dict) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return "エラー: url を指定してください"
        cmd = ["sqlmap", "-u", _shquote(url), "--batch", "--flush-session"]
        data = str(args.get("data", "")).strip()
        if data:
            cmd += ["--data", _shquote(data)]
        level = str(args.get("level", "")).strip()
        if level:
            cmd += ["--level", level]
        risk = str(args.get("risk", "")).strip()
        if risk:
            cmd += ["--risk", risk]
        extra = str(args.get("extra", "")).strip()
        if extra:
            cmd.append(extra)
        return _run_cmd(" ".join(cmd), timeout=600)


def _shquote(s: str) -> str:
    """簡易シェルクォート（スペースや特殊文字を含むURL/データ用）。"""
    if not s:
        return "''"
    if all(c.isalnum() or c in "-_./:?=&%~" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"
