# -*- coding: utf-8 -*-
#servers.py — 常駐コマンド（サーバ起動等）をバックグラウンドで管理する
"""
uvicorn --reload / flask run / npm start のような「終わらないコマンド」を検知し、
フォアグラウンドで待たずにバックグラウンド起動。数秒だけ出力を拾って即座に返す。
起動したプロセスは一覧・停止できる（UI/エージェントから）。
"""
from __future__ import annotations

import re
import subprocess
import shell
import threading
import time

# 常駐とみなすパターン（サーバ・監視・無限ループ系）
_LONG_RUNNING = re.compile(
    r"\b(uvicorn|gunicorn|hypercorn|flask\s+run|npm\s+(run\s+)?(start|dev|serve)|"
    r"yarn\s+(start|dev)|next\s+(dev|start)|vite|webpack(\s+serve)?|"
    r"http\.server|python\s+-m\s+http|rails\s+s|php\s+-S|serve\b|watch\b|"
    r"tail\s+-f|ping\b(?!.*-c)|nodemon)\b", re.I)
# 明示フラグ
_RELOAD_FLAG = re.compile(r"--reload\b|--watch\b|-w\b", re.I)

_PROCS: dict[int, dict] = {}     # pid -> {cmd, proc, started, output}
_lock = threading.Lock()


def is_long_running(cmd: str) -> bool:
    return bool(_LONG_RUNNING.search(cmd) or _RELOAD_FLAG.search(cmd))


def start_background(cmd: str, shell_prefix: list[str], cwd: str,
                     grab_seconds: float = 4.0) -> str:
    """常駐コマンドをバックグラウンド起動。数秒だけ出力を拾って返す。"""
    import os
    os.makedirs(cwd, exist_ok=True)
    try:
        proc = subprocess.Popen(
            shell_prefix + [cmd], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            **shell.no_window_kwargs())
    except Exception as ex:
        return f"エラー: 起動失敗: {ex}"

    buf: list[str] = []

    def _reader():
        try:
            for line in proc.stdout:
                buf.append(line)
                if len(buf) > 200:
                    buf.pop(0)
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    time.sleep(grab_seconds)        # 起動直後の出力を少しだけ拾う

    with _lock:
        _PROCS[proc.pid] = {"cmd": cmd, "proc": proc,
                            "started": time.strftime("%H:%M:%S"), "buf": buf}

    if proc.poll() is not None:     # すぐ終了した＝エラーの可能性
        return (f"プロセスは即終了しました(code={proc.returncode})。\n"
                + "".join(buf[-20:]))
    head = "".join(buf[-15:]) or "(まだ出力なし)"
    return (f"OK: バックグラウンドで起動しました（PID {proc.pid}）。"
            f"常駐プロセスなので待機せず次に進みます。\n--- 起動直後の出力 ---\n{head}"
            f"\n（停止するには ssh_disconnect ではなく type:\"server_stop\" pid={proc.pid}）")


def list_servers() -> list[dict]:
    out = []
    with _lock:
        for pid, info in list(_PROCS.items()):
            alive = info["proc"].poll() is None
            if not alive:
                continue
            out.append({"pid": pid, "cmd": info["cmd"],
                        "started": info["started"], "alive": alive})
    return out


def stop(pid: int) -> str:
    with _lock:
        info = _PROCS.get(pid)
    if not info:
        return f"エラー: PID {pid} は管理外です"
    try:
        info["proc"].terminate()
        try:
            info["proc"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            info["proc"].kill()
        with _lock:
            _PROCS.pop(pid, None)
        return f"OK: PID {pid} を停止しました"
    except Exception as ex:
        return f"エラー: 停止失敗: {ex}"


def stop_all() -> str:
    pids = list(_PROCS)
    for pid in pids:
        stop(pid)
    return f"OK: {len(pids)}個のプロセスを停止しました"
