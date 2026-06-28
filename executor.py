# -*- coding: utf-8 -*-
#executor.py — 実行レイヤー（system役の行動JSONを実際に実行する）
"""
agent_loop の execute_action がここを呼ぶ。
行動JSON（json_checker.handle_task を通ったもの）を受け取り、実際に実行して
結果文字列を返す。安全のため、副作用のある操作は必ずユーザー承認を挟む。

  result = run_action(action)            # 対話承認つき
  result = run_action(action, approver=auto_yes)  # テスト用に承認を差し替え可

対応:
  command : シェルコマンド実行（承認必須・タイムアウトつき）
  file    : read / write / append / delete（write/append/delete は承認必須）
  code    : python等のコードを一時ファイルに保存して実行（承認必須）
  assist  : AIからの質問をユーザーに尋ね、回答を結果として返す
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import shell
import installs
import ssh_session
import servers

WORKSPACE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspace")

def _resolve(path: str) -> str:
    """file操作のpathを必ず workspace/ 配下に解決する（../ や絶対パス脱出を防ぐ）。
    絶対パスや ../ を含む脱出パスは、拒否せず workspace 配下のファイル名へ寄せる
    （LLMが /tmp/x.txt 等を指定しても安全な場所へ自動リダイレクト）。"""
    os.makedirs(WORKSPACE, exist_ok=True)
    p = os.path.abspath(os.path.join(WORKSPACE, os.path.basename(path)
                                     if os.path.isabs(path) else path))
    if not p.startswith(WORKSPACE):
        # 脱出を試みた相対パスは、ベース名だけ取って workspace 配下へ寄せる
        p = os.path.abspath(os.path.join(WORKSPACE, os.path.basename(path)))
        if not p.startswith(WORKSPACE):
            raise ValueError("workspace外への操作は禁止（workspace内のファイル名で指定してください）")
    return p

COMMAND_TIMEOUT = 600    # 秒（インストール等を許容して長め）
CODE_TIMEOUT = 600
OUTPUT_LIMIT = 4000
# 隔離環境なら True で全操作を無確認実行（環境変数 AGENT_AUTO_APPROVE=1 でも可）
AUTO_APPROVE = os.environ.get("AGENT_AUTO_APPROVE") == "1"      # 結果文字列の上限（長大な出力で記憶を溢れさせない）

# code の language → 実行コマンド
_RUNNERS = {
    "python": [sys.executable],
    "py": [sys.executable],
    "bash": ["bash"],
    "sh": ["sh"],
}


def _truncate(text: str) -> str:
    text = text or ""
    if len(text) > OUTPUT_LIMIT:
        return text[:OUTPUT_LIMIT] + f"\n…(出力を{OUTPUT_LIMIT}字で切り詰め)"
    return text


def _ask_approval(prompt: str) -> bool:
    """対話でユーザー承認を取る。AUTO_APPROVE時は無確認で許可。"""
    if AUTO_APPROVE:
        return True
    try:
        ans = input(f"{prompt} 実行しますか? [y/N] > ").strip().lower()
    except EOFError:
        return False
    return ans == "y"


def auto_yes(_prompt: str) -> bool:
    """テスト用：常に承認。"""
    return True


def auto_no(_prompt: str) -> bool:
    """テスト用：常に拒否。"""
    return False


# ----------------------------------------------------------------------- #
# 各 type の実行
# ----------------------------------------------------------------------- #
def _pull_remote_outputs(cmd: str) -> str:
    """Kali上のコマンドが出力ファイルを作った場合、その中身をローカルworkspaceに控える。
    対象: リダイレクト( > file ), nmap -oN/-oA/-oX/-oG, tee file 等。"""
    import re
    paths = []
    # > file / >> file
    for m in re.finditer(r">>?\s*([^\s;|&>]+)", cmd):
        paths.append(m.group(1))
    # nmap -oN/-oX/-oG/-oA <base> , -o <file>
    for m in re.finditer(r"-o[NXGA]?\s+([^\s;|&]+)", cmd):
        paths.append(m.group(1))
    # tee [-a] file
    for m in re.finditer(r"\btee\s+(?:-a\s+)?([^\s;|&]+)", cmd):
        paths.append(m.group(1))
    if not paths:
        return ""
    os.makedirs(WORKSPACE, exist_ok=True)
    saved = []
    for rpath in dict.fromkeys(paths):           # 重複除去・順序維持
        rpath = rpath.strip("'\"")
        if not rpath or rpath.startswith("/dev/") or rpath == "-":
            continue
        content = ssh_session.run(f"cat {rpath} 2>/dev/null", timeout=60)
        if content.startswith("エラー:") or not content.strip():
            continue
        local = os.path.join(WORKSPACE, "kali_" + os.path.basename(rpath))
        try:
            with open(local, "w", encoding="utf-8", errors="replace") as f:
                f.write(content)
            saved.append(os.path.basename(local))
        except OSError:
            pass
    if saved:
        return f"[Kaliの出力をローカルに保存: {', '.join(saved)}（workspace内）]\n"
    return ""


def _substitute_target_placeholders(cmd: str) -> str:
    """コマンド内の未展開プレースホルダ（$TARGET_IP 等）を実ターゲットに置換する。
    LLMが $TARGET_IP/$TARGET_HOST/<IP> 等をそのまま出すと、シェルでは空文字に
    展開されて『No targets specified』で空振りするため、ここで実値に差し替える。"""
    if not cmd or "$" not in cmd and "<" not in cmd and "{" not in cmd:
        return cmd
    ctx = {}
    try:
        import sys
        al = sys.modules.get("agent_loop")
        if al is not None:
            ctx = getattr(al, "_TARGET", {}).get("ctx") or {}
    except Exception:
        ctx = {}
    primary = ctx.get("primary_target", "") if ctx else ""
    ip = ctx.get("primary_ip", "") if ctx else ""
    if not primary and not ip:
        return cmd
    host_val = primary or ip
    ip_val = ip or primary
    import re as _re
    # re.sub の replacement文字列は \1 等をグループ参照と解釈しcrashするため、
    # lambda で固定文字列を返して安全に置換する。
    def _rep(val):
        return lambda m: val
    # $TARGET_IP / ${TARGET_IP} / $TARGETIP は IP（無ければhost）に
    cmd = _re.sub(r"\$\{?TARGET_?IP\}?", _rep(ip_val), cmd, flags=_re.I)
    # $TARGET_HOST / $TARGETHOST / $TARGET_DOMAIN は host に
    cmd = _re.sub(r"\$\{?TARGET_?(HOST|DOMAIN|URL)\}?", _rep(host_val), cmd, flags=_re.I)
    # $TARGET / ${TARGET} は host に
    cmd = _re.sub(r"\$\{?TARGET\}?", _rep(host_val), cmd, flags=_re.I)
    cmd = _re.sub(r"\$\{?RHOST[S]?\}?", _rep(ip_val), cmd, flags=_re.I)
    # <IP> <TARGET> <HOST> <target_ip> 等の山括弧プレースホルダ
    cmd = _re.sub(r"<\s*(target[_ ]?ip|rhost)\s*>", _rep(ip_val), cmd, flags=_re.I)
    cmd = _re.sub(r"<\s*(ip|target|host|target[_ ]?host|domain|url)\s*>",
                  _rep(host_val), cmd, flags=_re.I)
    return cmd


def _run_command(action: dict, approver) -> str:
    cmd = action.get("command", "")
    if not cmd:
        return "エラー: commandが空"
    # 未展開プレースホルダ（$TARGET_IP 等）を実ターゲットに置換（空振り防止）
    cmd = _substitute_target_placeholders(cmd)
    # SSH接続中ならリモート(Kali等)で実行する
    if ssh_session.is_connected():
        if not approver(f"SSH実行({ssh_session.status()['host']}): {cmd}"):
            return "ユーザーが実行を拒否しました"
        # 対話型コマンド用の入力（LLMが "input" に指定。例: yes応答やパスワード）
        input_text = action.get("input")
        if input_text and not input_text.endswith("\n"):
            input_text += "\n"
        out = ssh_session.run(cmd, timeout=COMMAND_TIMEOUT, input_text=input_text)
        ok = not out.startswith("エラー:")
        rec = installs.record(cmd, ok, where=ssh_session.status()["host"])
        note = ""
        if rec:
            note = f"[記録: {rec['manager']} {', '.join(rec['packages'])} を{'導入' if ok else '導入失敗'}@{rec['where']}]\n"
        # Kali側で出力ファイルが作られた場合、ローカルにも控えを取り戻す（手元に残す）
        pulled = _pull_remote_outputs(cmd)
        if pulled:
            note += pulled
        return note + (_truncate(out) or "(出力なし)")
    if not approver(f"コマンド({shell.shell_name()}): {cmd}"):
        return "ユーザーが実行を拒否しました"
    os.makedirs(WORKSPACE, exist_ok=True)   # 実行カレントを用意
    # 常駐コマンド（uvicorn --reload 等）は待たずにバックグラウンド起動（無限ループ防止）
    if servers.is_long_running(cmd):
        return servers.start_background(cmd, shell.shell_prefix(), WORKSPACE)
    try:
        # OSに応じたシェル（Windows=PowerShell / Linux=bash 等）で実行
        # text=Trueだけだと出力がOS既定エンコーディング(Windows=cp932)で
        # デコードされ日本語が文字化けする。UTF-8を明示して防ぐ。
        proc = subprocess.run(
            shell.shell_prefix() + [cmd], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=COMMAND_TIMEOUT, cwd=WORKSPACE,
            **shell.no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return f"エラー: タイムアウト（{COMMAND_TIMEOUT}秒）"
    ok = proc.returncode == 0
    rec = installs.record(cmd, ok)          # pip/apt/npm等のインストールを記録
    note = ""
    if rec:
        note = f"[記録: {rec['manager']} {', '.join(rec['packages'])} を{'導入' if ok else '導入失敗'}]\n"
    if ok:
        return note + (_truncate(proc.stdout) or "(出力なし・成功)")
    return note + _truncate(f"エラー(code={proc.returncode}): {proc.stderr or proc.stdout}")


def _run_ssh_connect(action: dict, approver) -> str:
    host = action.get("host"); user = action.get("user")
    if not host or not user:
        return "エラー: host と user が必要です"
    if not approver(f"SSH接続: {user}@{host}"):
        return "ユーザーが接続を拒否しました"
    return ssh_session.connect(host, user, int(action.get("port", 22)),
                               action.get("password"), action.get("key_path"))


def _run_server_stop(action: dict, approver) -> str:
    pid = action.get("pid")
    if pid is None:
        return servers.stop_all()
    return servers.stop(int(pid))


def _run_server_list(action: dict, approver) -> str:
    import json as _j
    return _j.dumps(servers.list_servers(), ensure_ascii=False)


def _run_tool(action: dict, approver) -> str:
    import tools.registry as tr
    name = action.get("name", "")
    args = action.get("args", {}) or {}
    # 登録ツール（calculator/cve_lookup/report等）はそのハンドラで実行
    if name in _registered_tool_names(tr):
        if not approver(f"ツール実行: {name}({args})"):
            return "ユーザーが実行を拒否しました"
        return _truncate(tr.run_tool(name, args))
    # それ以外は「任意のCLIツール名」とみなし、シェルコマンドに変換して実行する。
    # 使用するツールは制限しない（nmap/amass/feroxbuster/ユーザー導入の任意ツール等）。
    cli = _tool_to_shell(name, args, action)
    if cli:
        # コマンドが「ツール名のみ」（対象なし）の場合、現在のターゲットを補う。
        if cli.strip() == name.strip().lower():
            tgt = _current_primary_target()
            if tgt:
                cli = f"{cli} {tgt}"
        return _run_command({"type": "command", "command": cli}, approver)
    # name が空など、コマンド化できない場合のみツール実行（エラー提示）にフォールバック
    if not approver(f"ツール実行: {name}({args})"):
        return "ユーザーが実行を拒否しました"
    return _truncate(tr.run_tool(name, args))


def _registered_tool_names(tr) -> list:
    try:
        return tr.tool_names()
    except Exception:
        return []


def _current_primary_target() -> str:
    """実行中のrunの主ターゲットを取得（CLIコマンドに対象が無い時の補完用）。
    agent_loop を遅延参照し、循環import・未設定でも安全に空文字を返す。"""
    try:
        import sys
        al = sys.modules.get("agent_loop")
        if al is None:
            return ""
        ctx = getattr(al, "_TARGET", {}).get("ctx")
        if ctx and ctx.get("primary_target"):
            return ctx["primary_target"]
        info = getattr(al, "_TARGET", {}).get("info")
        if info:
            return info.get("ip") or info.get("host") or ""
    except Exception:
        pass
    return ""



def _tool_to_shell(name: str, args, action: dict):
    """ツール名+引数を shell コマンド文字列に変換する。
    使用するツールは制限しない（任意のCLIツール名を受け付ける）。
    name がコマンド名として不正（空・空白・パイプ等を含む）な場合のみ None。"""
    base = (name or "").strip()
    if not base:
        return None
    # name 自体が既に複数トークン（"nmap -sV uuum.jp" のような丸ごと指定）なら
    # そのままコマンドとして扱う
    if any(ch in base for ch in (" ", "\t")):
        return base
    base_l = base.lower()
    # args が文字列そのもの（例 "-sV uuum.jp"）のケース
    if isinstance(args, str):
        a = args.strip()
        if a:
            return a if a.split()[0] == base_l else f"{base} {a}"
        return base
    # args が直接リスト（例 ["-sV","uuum.jp"]）のケース
    if isinstance(args, (list, tuple)):
        if args:
            return base + " " + " ".join(str(a) for a in args)
        return base
    if not isinstance(args, dict):
        args = {}
    # 1) args/action に command/cmd/raw があれば最優先で使う
    for k in ("command", "cmd", "raw", "cmdline", "full_command"):
        v = args.get(k) or action.get(k)
        if v:
            v = str(v).strip()
            return v if v.split() and v.split()[0] == base_l else f"{base} {v}"
    # 2) 引数群が list/str のキー
    for ak in ("args", "argv", "flags", "arguments", "params", "parameters"):
        argv = args.get(ak)
        if isinstance(argv, (list, tuple)) and argv:
            return base + " " + " ".join(str(a) for a in argv)
        if isinstance(argv, str) and argv.strip():
            return f"{base} {argv.strip()}"
    # 3) よくあるキー（target/host/url/options 等）から組み立て
    opts = (args.get("options") or args.get("flags") or args.get("opts")
            or args.get("scan_type") or args.get("scanType") or "")
    target = (args.get("target") or args.get("host") or args.get("url")
              or args.get("ip") or args.get("domain") or args.get("hostname")
              or args.get("address") or "")
    port = args.get("port") or args.get("ports") or ""
    parts = [base]
    if opts:
        parts.append(str(opts).strip())
    if port and base_l in ("nmap", "masscan", "rustscan"):
        parts.append(f"-p {port}")
    if target:
        parts.append(str(target).strip())
    # 残りの任意キーも "key value" でなく値だけ拾う（汎用性のため）
    cmd = " ".join(p for p in parts if p)
    if cmd != base:
        return cmd
    # 引数の手がかりが何も無くても、ツール名だけは返す（呼び出し側が対象を補完）
    return base


def _run_ssh_disconnect(action: dict, approver) -> str:
    return ssh_session.disconnect()


def _run_file(action: dict, approver) -> str:
    act = action.get("action", "") or "write"   # 省略時は write（プロンプト契約に整合）
    # LLMがよく使う別名を正規化（create/new/save→write, cat/show→read, remove/rm→delete）
    _ALIASES = {"create": "write", "new": "write", "save": "write",
                "overwrite": "write", "make": "write",
                "cat": "read", "show": "read", "open": "read", "view": "read",
                "remove": "delete", "rm": "delete", "del": "delete"}
    act = _ALIASES.get(act.lower(), act.lower())
    path = action.get("path", "")
    if not path:
        return "エラー: pathが空"
    try:
        path = _resolve(path)   # ★生成物は必ず workspace/ へ
    except ValueError as ex:
        return f"エラー: {ex}"

    if act == "read":
        try:
            with open(path, "r", encoding="utf-8") as f:
                return _truncate(f.read())
        except OSError as e:
            return f"エラー: 読み込み失敗 {e}"

    if act in ("write", "append"):
        if not approver(f"ファイル{act}: {path}"):
            return "ユーザーが操作を拒否しました"
        mode = "w" if act == "write" else "a"
        try:
            with open(path, mode, encoding="utf-8") as f:
                f.write(action.get("content", ""))
            return f"OK: {path} に{act}しました"
        except OSError as e:
            return f"エラー: 書き込み失敗 {e}"

    if act == "delete":
        if not approver(f"ファイル削除: {path}"):
            return "ユーザーが操作を拒否しました"
        try:
            os.remove(path)
            return f"OK: {path} を削除しました"
        except OSError as e:
            return f"エラー: 削除失敗 {e}"

    return f"エラー: 未知のfile action: {act}"


def _run_code(action: dict, approver) -> str:
    lang = (action.get("language") or "python").lower()
    code = action.get("code", "")
    if not code:
        return "エラー: codeが空"
    runner, _suffix = shell.code_runner(lang)
    if runner is None:
        return f"エラー: この環境では未対応の言語: {lang}（OS={shell.SYSTEM}）"

    # path があれば「成果物としてworkspaceに保存」する（残らないと意味がないコード生成用）
    path = action.get("path")
    saved_note = ""
    if path:
        try:
            saved = _resolve(path)
        except ValueError as ex:
            return f"エラー: {ex}"
        if not approver(f"コードを保存: {path}"):
            return "ユーザーが保存を拒否しました"
        try:
            with open(saved, "w", encoding="utf-8") as f:
                f.write(code)
            saved_note = f"OK: {saved} に保存しました"
        except OSError as ex:
            return f"エラー: 保存失敗 {ex}"

    # run=False（既定はpathがあれば保存のみ／なければ実行）。明示的に実行可否を選べる
    run = action.get("run", path is None)
    if not run:
        return saved_note or "OK: コードを生成しました（実行はしていません）"

    if not approver(f"{lang}コードを実行:\n{code[:200]}"):
        return (saved_note + "\n" if saved_note else "") + "ユーザーが実行を拒否しました"

    suffix = _suffix
    tmp = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8")
    try:
        tmp.write(code)
        tmp.close()
        proc = subprocess.run(
            runner + [tmp.name], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=CODE_TIMEOUT,
            **shell.no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return f"エラー: タイムアウト（{CODE_TIMEOUT}秒）"
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass

    head = (saved_note + "\n") if saved_note else ""
    if proc.returncode == 0:
        return head + (_truncate(proc.stdout) or "(出力なし・成功)")
    return head + _truncate(f"エラー(code={proc.returncode}): {proc.stderr or proc.stdout}")


def _run_assist(action: dict, approver) -> str:
    msg = action.get("message", "（質問なし）")
    try:
        return input(f"AIからの確認: {msg} > ").strip() or "(回答なし)"
    except EOFError:
        return "(回答なし)"


def _run_web_search(action: dict, approver) -> str:
    """web_search型: engine で検索し、結果テキストを返す（承認不要・副作用なし）。"""
    query = action.get("query", "")
    if not query:
        return "エラー: queryが空"
    try:
        from engine import search
    except Exception as e:                       # noqa: BLE001
        return f"エラー: engine読込失敗 {e}"
    eng = action.get("engine")                   # 任意指定。無ければ既定＋フォールバック
    deep = action.get("deep", True)              # 既定で深掘り（LLMが重要リンクを選び実ページ取得）
    if deep:
        from engine import deep_search
        return _truncate(deep_search(query, limit=5, engine=eng))
    resp = search(query, limit=5, engine=eng)
    return _truncate(resp.to_text())


_DISPATCH = {
    "command": _run_command,
    "file": _run_file,
    "code": _run_code,
    "assist": _run_assist,
    "web_search": _run_web_search,
    "ssh_connect": _run_ssh_connect,
    "ssh_disconnect": _run_ssh_disconnect,
    "tool": _run_tool,
    "server_stop": _run_server_stop,
    "server_list": _run_server_list,
}


def run_action(action: dict, approver=_ask_approval, dry_run: bool = False) -> str:
    """
    行動JSONを実行して結果文字列を返す。
    approver(prompt)->bool で承認方法を差し替え可。dry_run=True なら実行せず計画のみ返す。
    """
    action = _normalize_action_type(action)
    t = action.get("type")
    if dry_run:
        detail = (action.get("command") or action.get("path")
                  or action.get("language") or action.get("message") or "")
        # ドライランでも「計画は妥当＝成功とみなす」結果を返す。
        # （未実行を強調すると judge が永久に未完了と判断しループするため）
        return f"OK（ドライラン・シミュレート成功）{t}: {detail}"
    handler = _DISPATCH.get(t)
    if handler is None:
        return f"エラー: 未知のtype: {t}"
    return handler(action, approver)


# LLMがtypeにファイル操作名やツール名を直接入れる誤りを正規化する。
# 例: {"type":"create","path":...} → {"type":"file","action":"create",...}
_FILE_ACTION_TYPES = {"create", "write", "read", "append", "delete",
                      "new", "save", "overwrite", "make", "cat", "show",
                      "open", "view", "remove", "rm", "del", "edit"}


def _normalize_action_type(action: dict) -> dict:
    if not isinstance(action, dict):
        return action
    t = (action.get("type") or "").lower().strip()
    if t in _DISPATCH:
        return action
    # ファイル操作名がtypeに来た場合 → type=file, action=元のtype へ振り替え
    if t in _FILE_ACTION_TYPES:
        a = dict(action)
        a["action"] = action.get("action") or t
        a["type"] = "file"
        return a
    # よくある別名: shell/bash/cmd/run → command, python → code, search → web_search
    _TYPE_ALIASES = {"shell": "command", "bash": "command", "cmd": "command",
                     "run": "command", "exec": "command", "execute": "command",
                     "python": "code", "script": "code",
                     "search": "web_search", "websearch": "web_search",
                     "ssh": "ssh_connect"}
    if t in _TYPE_ALIASES:
        a = dict(action)
        a["type"] = _TYPE_ALIASES[t]
        if a["type"] == "code" and not a.get("language"):
            a["language"] = "python"
        return a
    return action


if __name__ == "__main__":
    # 自動承認でひと通り動作確認（安全な範囲のみ）
    print(run_action({"type": "command", "command": "echo hello"}, approver=auto_yes))
    print(run_action({"type": "command", "command": "echo secret"}, approver=auto_no))
    print(run_action({"type": "file", "action": "write", "path": "/tmp/_ex_test.txt",
                      "content": "data"}, approver=auto_yes))
    print(run_action({"type": "file", "action": "read", "path": "/tmp/_ex_test.txt"}))
    print(run_action({"type": "code", "language": "python",
                      "code": "print(2+3)"}, approver=auto_yes))
    print(run_action({"type": "unknown"}))
