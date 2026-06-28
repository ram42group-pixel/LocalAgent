# -*- coding: utf-8 -*-
#tools/builtins.py — 標準で使えるツール群
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request

from tools.base import Tool


class CalculatorTool(Tool):
    name = "calculator"
    description = "数式を計算する。例: 2*(3+4), 15%4"
    args = {"expr": "計算する数式（Python算術式）"}

    def run(self, args: dict) -> str:
        expr = str(args.get("expr", ""))
        if not re.fullmatch(r"[0-9+\-*/%.()\s]+", expr):
            return "エラー: 使える文字は数字と + - * / % . ( ) のみ"
        try:
            return f"{expr} = {eval(expr, {'__builtins__': {}}, {})}"
        except Exception as ex:
            return f"エラー: {ex}"


class HttpGetTool(Tool):
    name = "http_get"
    description = "URLにGETして本文の先頭を取得する（API確認・ヘッダ取得など）"
    args = {"url": "取得するURL", "limit": "最大文字数(任意,既定1500)"}

    def run(self, args: dict) -> str:
        url = str(args.get("url", ""))
        if not url.startswith(("http://", "https://")):
            return "エラー: http(s)のURLを指定してください"
        limit = int(args.get("limit", 1500) or 1500)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LocalAgent/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read(limit * 4).decode("utf-8", errors="replace")
            return f"[{getattr(r,'status','?')}] " + body[:limit]
        except Exception as ex:
            return f"エラー: {ex}"


class FileSummarizeTool(Tool):
    name = "file_summarize"
    description = "ワークスペース内のファイルの行数・先頭・末尾を要約する"
    args = {"path": "ワークスペース相対パス"}

    def run(self, args: dict) -> str:
        import executor
        path = str(args.get("path", ""))
        try:
            full = path if os.path.isabs(path) else os.path.join(executor.WORKSPACE, path)
            if not os.path.exists(full):
                return f"エラー: 見つからない: {path}"
            with open(full, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            head = "".join(lines[:8]); tail = "".join(lines[-5:]) if len(lines) > 8 else ""
            return f"{path}: {len(lines)}行\n--- 先頭 ---\n{head}" + (f"--- 末尾 ---\n{tail}" if tail else "")
        except Exception as ex:
            return f"エラー: {ex}"


class GitTool(Tool):
    name = "git"
    description = "git の読み取り系コマンドを実行（status/log/diff/branch）"
    args = {"subcommand": "status / log / diff / branch のいずれか"}

    def run(self, args: dict) -> str:
        import shell
        sub = str(args.get("subcommand", "status")).strip()
        allowed = {"status": "git status -s", "log": "git log --oneline -10",
                   "diff": "git diff --stat", "branch": "git branch -a"}
        cmd = allowed.get(sub)
        if not cmd:
            return f"エラー: 使えるのは {', '.join(allowed)}"
        import executor
        try:
            proc = subprocess.run(shell.shell_prefix() + [cmd], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=30, cwd=executor.WORKSPACE,
                                  **shell.no_window_kwargs())
            return proc.stdout or proc.stderr or "(出力なし)"
        except Exception as ex:
            return f"エラー: {ex}"


class AppDetectTool(Tool):
    name = "app_detect"
    description = ("FastAPI/Flask/Starlette等のASGI/WSGIアプリ変数を検出する。"
                  "uvicorn起動用の module:app 文字列を返す")
    args = {"path": "対象Pythonファイル（ワークスペース相対）"}

    def run(self, args: dict) -> str:
        import executor as _ex
        path = str(args.get("path", ""))
        full = path if os.path.isabs(path) else os.path.join(_ex.WORKSPACE, path)
        if not os.path.exists(full):
            return f"エラー: 見つからない: {path}"
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                src = f.read()
        except Exception as ex:
            return f"エラー: {ex}"
        # 変数 = FastAPI()/Starlette()/Flask() を探す
        pats = [(r"(\w+)\s*=\s*FastAPI\s*\(", "FastAPI"),
                (r"(\w+)\s*=\s*Starlette\s*\(", "Starlette"),
                (r"(\w+)\s*=\s*Flask\s*\(", "Flask")]
        found = []
        for pat, kind in pats:
            for m in re.finditer(pat, src):
                found.append((m.group(1), kind))
        if not found:
            return f"{path}: ASGI/WSGIアプリ変数は見つかりませんでした"
        # module名 = 拡張子なしファイル名（サブディレクトリは . 区切り）
        rel = os.path.relpath(full, _ex.WORKSPACE) if not os.path.isabs(path) else os.path.basename(full)
        module = rel.replace(os.sep, ".").rsplit(".py", 1)[0]
        var, kind = found[0]
        runner = "uvicorn" if kind in ("FastAPI", "Starlette") else "flask run"
        cmds = []
        for var, kind in found:
            if kind in ("FastAPI", "Starlette"):
                cmds.append(f"uvicorn {module}:{var}")
        result = f"{path}: 検出 {[f'{v}({k})' for v,k in found]}\n"
        result += f"推奨起動: {cmds[0] if cmds else 'flask run'}"
        return result
