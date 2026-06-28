# -*- coding: utf-8 -*-
#ssh_session.py — SSHで遠隔ホスト(Kali等)に接続し、セッションを保持する
"""
方式は2系統を自動選択：
  1) paramiko があれば永続SSHセッション。対話型(sudo等)にも対応。
  2) 無ければ subprocess で ssh を都度実行（接続情報を保持＝擬似セッション）。
     パスワード認証は sshpass があれば対応。

対話型コマンド対応：run(cmd, input_text=...) で標準入力へ流し込む。
sudo は接続時パスワードを自動で渡す（sudo -S）。
"""
from __future__ import annotations

import re
import subprocess
import shell

_STATE = {
    "connected": False,
    "host": None, "user": None, "port": 22,
    "backend": None,      # "paramiko" / "subprocess"
    "client": None,       # paramiko.SSHClient
    "password": None,     # sudo等の対話入力に再利用
    "key_path": None,
}


def is_connected() -> bool:
    # キャッシュ上は接続済みでも、paramikoのトランスポートが落ちていることがある。
    # その場合は1度だけ自動再接続を試み、接続状態を正確に反映する。
    if _STATE["connected"] and _STATE["backend"] == "paramiko":
        if not _is_transport_alive():
            _reconnect()      # 失敗しても _STATE は connect 内で更新される
    return _STATE["connected"]


def _is_transport_alive() -> bool:
    """paramikoのトランスポートが実際に生きているか確認する。
    keepaliveでも切れることはあるため、実行直前にチェックする。"""
    if _STATE["backend"] != "paramiko":
        return _STATE["connected"]
    cli = _STATE.get("client")
    if cli is None:
        return False
    try:
        tr = cli.get_transport()
        return bool(tr and tr.is_active())
    except Exception:
        return False


def _reconnect() -> bool:
    """保存済みの認証情報で再接続を試みる（タスク中の切断からの自動復旧）。
    成功で True。disconnect→connect を使うため keepalive も再設定される。"""
    host = _STATE.get("host")
    user = _STATE.get("user")
    port = _STATE.get("port", 22)
    pw = _STATE.get("password")
    key = _STATE.get("key_path")
    if not host or not user:
        return False
    res = connect(host, user, port, password=pw, key_path=key)
    return _STATE["connected"] and res.startswith("OK")


def status() -> dict:
    return {k: _STATE[k] for k in ("connected", "host", "user", "port", "backend")}


def describe() -> str:
    if not _STATE["connected"]:
        return ""
    return f"SSH接続中: {_STATE['user']}@{_STATE['host']}:{_STATE['port']}（backend={_STATE['backend']}）"


def connect(host: str, user: str, port: int = 22,
            password: str | None = None, key_path: str | None = None) -> str:
    """SSH接続。paramiko優先、無ければsubprocess。失敗理由を詳しく返す。"""
    disconnect()
    if not host or not user:
        return "エラー: ホストとユーザを指定してください"
    port = int(port or 22)

    paramiko_err = None
    try:
        import paramiko
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"hostname": host, "port": port, "username": user, "timeout": 15,
              "allow_agent": True, "look_for_keys": True}
        if key_path:
            kw["key_filename"] = key_path
        if password:
            kw["password"] = password
            kw["look_for_keys"] = False
        cli.connect(**kw)
        # キープアライブ: アイドル時もNAT/FW/サーバにTCPを生かし続け、
        # 長時間タスク中やコマンド間の無通信での切断を防ぐ。
        try:
            tr = cli.get_transport()
            if tr is not None:
                tr.set_keepalive(15)        # 15秒ごとに keepalive を送る
        except Exception:
            pass
        _STATE.update(connected=True, host=host, user=user, port=port,
                      backend="paramiko", client=cli, password=password,
                      key_path=key_path)
        return f"OK: paramikoで {user}@{host}:{port} に接続しました"
    except ImportError:
        paramiko_err = "paramiko未導入"
    except Exception as ex:
        return (f"エラー: paramiko接続失敗: {ex}\n"
                "ヒント: ホスト/ユーザ/ポート、パスワードまたは鍵、"
                "Kali側でsshd起動(sudo systemctl start ssh)を確認してください。")

    if password:
        if _has_sshpass():
            try:
                test = subprocess.run(
                    ["sshpass", "-p", password] +
                    _ssh_argv(host, user, port, key_path, "echo __ok__", batch=False),
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
                if "__ok__" in test.stdout:
                    _STATE.update(connected=True, host=host, user=user, port=port,
                                  backend="subprocess", client=None,
                                  password=password, key_path=key_path)
                    return f"OK: sshpassで {user}@{host}:{port} に接続を確認しました"
                return f"エラー: ssh疎通失敗: {test.stderr.strip() or test.stdout.strip()}"
            except Exception as ex:
                return f"エラー: sshpass接続失敗: {ex}"
        return (f"エラー: パスワード認証には paramiko か sshpass が必要です（{paramiko_err}）。"
                "`pip install paramiko` か、鍵認証（鍵ファイルパス指定）を使ってください。")
    try:
        test = subprocess.run(
            _ssh_argv(host, user, port, key_path, "echo __ok__", batch=True),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        if "__ok__" in test.stdout:
            _STATE.update(connected=True, host=host, user=user, port=port,
                          backend="subprocess", client=None, password=None,
                          key_path=key_path)
            return f"OK: ssh(subprocess)で {user}@{host}:{port} に接続を確認しました"
        return (f"エラー: ssh疎通失敗: {test.stderr.strip() or test.stdout.strip() or '不明'}\n"
                "ヒント: 鍵認証の設定とホスト到達性を確認してください。")
    except FileNotFoundError:
        return f"エラー: paramikoもsshも見つかりません（{paramiko_err}。pip install paramiko 推奨）"
    except subprocess.TimeoutExpired:
        return "エラー: 接続タイムアウト（到達性/ポート/ファイアウォールを確認）"
    except Exception as ex:
        return f"エラー: ssh接続失敗: {ex}"


def _has_sshpass() -> bool:
    try:
        subprocess.run(["sshpass", "-V"], capture_output=True, timeout=5, **shell.no_window_kwargs())
        return True
    except Exception:
        return False


def _ssh_argv(host, user, port, key_path, remote_cmd, batch=True) -> list[str]:
    argv = ["ssh", "-p", str(port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            # キープアライブと接続タイムアウト: 長時間タスク中の切断を防ぐ
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-o", "ConnectTimeout=15"]
    if batch:
        argv += ["-o", "BatchMode=yes"]
    if key_path:
        argv += ["-i", key_path]
    argv += [f"{user}@{host}", remote_cmd]
    return argv


def _looks_interactive(cmd: str) -> bool:
    return bool(re.search(r"\bsudo\b", cmd))


def run(cmd: str, timeout: int = 600, input_text: str | None = None) -> str:
    """接続中のSSHでコマンド実行。input_textで標準入力へ流し込む（対話型対応）。
    sudo は接続時パスワードを自動で渡す（sudo -S）。"""
    if not _STATE["connected"]:
        return "エラー: SSH未接続です。先に ssh_connect で接続してください"

    pw = _STATE.get("password")
    if _looks_interactive(cmd) and pw and "sudo -S" not in cmd:
        cmd = cmd.replace("sudo ", "sudo -S ", 1)
        if input_text is None:
            input_text = pw + "\n"

    if _STATE["backend"] == "paramiko":
        # 実行直前に接続が生きているか確認。死んでいれば1度だけ自動再接続。
        if not _is_transport_alive():
            if _reconnect():
                pass   # 復旧成功
            else:
                return ("エラー: SSH接続が切れ、自動再接続にも失敗しました。"
                        "ssh_connect で再接続してください")
        try:
            stdin, out, err = _STATE["client"].exec_command(
                cmd, timeout=timeout, get_pty=_looks_interactive(cmd))
            if input_text:
                try:
                    stdin.write(input_text)
                    stdin.flush()
                    stdin.channel.shutdown_write()
                except Exception:
                    pass
            o = out.read().decode(errors="replace")
            e = err.read().decode(errors="replace")
            e = e.replace(f"[sudo] password for {_STATE.get('user','')}: ", "")
            return o + (f"\n[stderr] {e}" if e.strip() else "")
        except Exception as ex:
            # 実行中に切れた可能性 → 1度だけ再接続して再実行を試みる
            if _reconnect():
                try:
                    stdin, out, err = _STATE["client"].exec_command(
                        cmd, timeout=timeout, get_pty=_looks_interactive(cmd))
                    if input_text:
                        try:
                            stdin.write(input_text)
                            stdin.flush()
                            stdin.channel.shutdown_write()
                        except Exception:
                            pass
                    o = out.read().decode(errors="replace")
                    e = err.read().decode(errors="replace")
                    e = e.replace(f"[sudo] password for {_STATE.get('user','')}: ", "")
                    return o + (f"\n[stderr] {e}" if e.strip() else "")
                except Exception as ex2:
                    return f"エラー: SSH実行失敗（再接続後も失敗）: {ex2}"
            return f"エラー: SSH実行失敗（接続切断、再接続不可）: {ex}"

    try:
        argv = _ssh_argv(_STATE["host"], _STATE["user"], _STATE["port"],
                         _STATE.get("key_path"), cmd, batch=(pw is None))
        if pw and _has_sshpass():
            argv = ["sshpass", "-p", pw] + argv
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, input=input_text,
                              **shell.no_window_kwargs())
        return proc.stdout + (f"\n[stderr] {proc.stderr}" if proc.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        return f"エラー: タイムアウト（{timeout}秒）。対話型なら input_text を指定してください。"
    except Exception as ex:
        return f"エラー: SSH実行失敗: {ex}"


def disconnect() -> str:
    if _STATE["backend"] == "paramiko" and _STATE["client"]:
        try:
            _STATE["client"].close()
        except Exception:
            pass
    was = _STATE["connected"]
    _STATE.update(connected=False, host=None, user=None, port=22,
                  backend=None, client=None, password=None, key_path=None)
    return "OK: SSH切断しました" if was else "（未接続）"
