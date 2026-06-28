# -*- coding: utf-8 -*-
#tools/mcp_client.py — 外部MCPサーバをツールとして利用する（依存ゼロ・stdio JSON-RPC）
"""
MCP(Model Context Protocol)サーバを子プロセスとして起動し、JSON-RPCで
ツール一覧取得・呼び出しを行う。標準ライブラリのみ（mcpパッケージ不要）。

設定は mcp_servers.json に保存：
  {"filesystem": {"command":"npx", "args":["-y","@modelcontextprotocol/server-filesystem","/path"]}}
"""
from __future__ import annotations

import json
import os
import subprocess
try:
    import shell
except Exception:
    shell=None
import threading

_CONF_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "mcp_servers.json")


def _load_conf() -> dict:
    try:
        with open(_CONF_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_conf(conf: dict) -> None:
    with open(_CONF_FILE, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)


def add_server(name: str, command: str, args: list[str]) -> dict:
    conf = _load_conf()
    conf[name] = {"command": command, "args": args}
    _save_conf(conf)
    return conf


def remove_server(name: str) -> dict:
    conf = _load_conf()
    conf.pop(name, None)
    _save_conf(conf)
    return conf


def list_servers() -> dict:
    return _load_conf()


class _McpProc:
    """MCPサーバの子プロセスとJSON-RPCでやり取りする最小クライアント。"""

    def __init__(self, command: str, args: list[str]):
        _nw = shell.no_window_kwargs() if shell else {}
        self.proc = subprocess.Popen(
            [command] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1, **_nw)
        self._id = 0
        self._lock = threading.Lock()

    def _rpc(self, method: str, params: dict | None = None, timeout: float = 30):
        with self._lock:
            self._id += 1
            req = {"jsonrpc": "2.0", "id": self._id, "method": method,
                   "params": params or {}}
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            # 該当idの応答を読むまでループ（通知は読み飛ばす）
            import time
            start = time.time()
            while time.time() - start < timeout:
                line = self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("id") == self._id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", "MCPエラー"))
                    return msg.get("result", {})
            raise TimeoutError("MCP応答タイムアウト")

    def _notify(self, method: str, params: dict | None = None):
        with self._lock:
            note = {"jsonrpc": "2.0", "method": method, "params": params or {}}
            self.proc.stdin.write(json.dumps(note) + "\n")
            self.proc.stdin.flush()

    def initialize(self):
        res = self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "LocalAgent", "version": "1.0"}})
        self._notify("notifications/initialized")
        return res

    def list_tools(self) -> list:
        return self._rpc("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        # content は [{type:text, text:...}] 形式
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts) or json.dumps(res, ensure_ascii=False)[:500]

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def list_remote_tools(server: str) -> list[dict]:
    """指定MCPサーバのツール一覧を取得。"""
    conf = _load_conf().get(server)
    if not conf:
        return []
    proc = _McpProc(conf["command"], conf.get("args", []))
    try:
        proc.initialize()
        return proc.list_tools()
    finally:
        proc.close()


def call_remote_tool(server: str, tool: str, arguments: dict) -> str:
    """指定MCPサーバのツールを1回呼び出す。"""
    conf = _load_conf().get(server)
    if not conf:
        return f"エラー: MCPサーバ '{server}' が未登録です"
    try:
        proc = _McpProc(conf["command"], conf.get("args", []))
    except FileNotFoundError:
        return f"エラー: コマンドが見つかりません: {conf['command']}"
    except Exception as ex:
        return f"エラー: MCPサーバ起動失敗: {ex}"
    try:
        proc.initialize()
        return proc.call_tool(tool, arguments or {})
    except Exception as ex:
        return f"エラー: MCP呼び出し失敗: {ex}"
    finally:
        proc.close()
