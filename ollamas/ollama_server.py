# -*- coding: utf-8 -*-
#ollama_server.py — ollamaサーバの自動起動（未起動なら立ち上げる）

import subprocess
import time
import shutil

# 一度起動を試みたら再試行しない（毎回subprocessを撒かないため）
_TRIED = {"done": False}


import os


def _ollama_base_url() -> str:
    """ollamaの接続先URL。OLLAMA_HOST を尊重し、無ければ既定のローカル。
    0.0.0.0 / :: 等の『全インターフェース待ち受け』指定は接続先に使えないため
    127.0.0.1 へ読み替える。実装は ollama_control._base_url と共通化。"""
    try:
        import ollamas.ollama_control as _ctl
        return _ctl._base_url()
    except Exception:
        pass
    # フォールバック（ollama_control を読めない場合のみ）
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return "http://127.0.0.1:11434"
    scheme = ""
    if host.startswith("http://") or host.startswith("https://"):
        scheme, host = host.split("://", 1)
        host = host.rstrip("/")
    port = ""
    h = host
    if h.startswith("[") and "]" in h:
        addr, _, rest = h.partition("]")
        h = addr.lstrip("[")
        if rest.startswith(":"):
            port = rest[1:]
    elif h.count(":") == 1:
        h, port = h.split(":", 1)
    if h in ("0.0.0.0", "::", "[::]", "*", ""):
        h = "127.0.0.1"
    if not port:
        port = "11434"
    return f"{scheme or 'http'}://{h}:{port}"


def is_ollama_running() -> bool:
    """ollamaサーバが応答するか確認する。
    - OLLAMA_HOST を尊重（既定 127.0.0.1:11434）
    - API エンドポイント /api/tags で確認
    - プロキシ環境でも localhost に直結（HTTP_PROXY等を無視）
    - requests（ユーザー環境で動作確認済み）優先、無ければ urllib"""
    base = _ollama_base_url()
    url = base.rstrip("/") + "/api/tags"
    # まず requests で /api/tags（プロキシを無効化して localhost に直結）
    try:
        import requests
        sess = requests.Session()
        sess.trust_env = False
        r = sess.get(url, timeout=3, proxies={"http": None, "https": None})
        return r.status_code == 200
    except ImportError:
        pass
    except Exception:
        return False
    # requests が無い場合のみ urllib
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_ollama() -> bool:
    """ollama serve をバックグラウンド起動して、起動を待つ。
    ollama 未インストールや起動失敗時は False を返す（例外で落とさない）。
    Windowsではコンソール窓を出さず、親プロセスから切り離して起動する。"""
    if shutil.which("ollama") is None:
        print("[ollama] コマンドが見つかりません（PATHを確認、またはクラウドLLMで動作）")
        return False
    print("[ollama] 起動を試みます: ollama serve ...")
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  stdin=subprocess.DEVNULL)
    try:
        if os.name == "nt":
            # Windowsでウィンドウが一瞬出るのを防ぐ。
            # CREATE_NO_WINDOW 単独で使う（DETACHED_PROCESS と併用すると
            # フラグが競合してコンソール窓が一瞬表示されることがある）。
            CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            CREATE_NEW_PROCESS_GROUP = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            # STARTUPINFO でもウィンドウ非表示を明示（二重の保険）
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0   # SW_HIDE
                kwargs["startupinfo"] = si
            except Exception:
                pass
        else:
            kwargs["start_new_session"] = True
    except Exception:
        pass
    try:
        # フルパスで起動（PATH上の .cmd シム経由を避け、余計な窓を出さない）
        _exe = shutil.which("ollama") or "ollama"
        proc = subprocess.Popen([_exe, "serve"], **kwargs)
    except Exception as ex:
        print(f"[ollama] 起動コマンドの実行に失敗: {ex}")
        return False
    for i in range(15):
        time.sleep(1)
        if is_ollama_running():
            print(f"[ollama] 起動を確認しました（{i + 1}秒）")
            return True
        rc = proc.poll()
        if rc is not None:
            if is_ollama_running():
                print("[ollama] 既存のインスタンスが起動中です")
                return True
            print(f"[ollama] serve プロセスが終了しました(code={rc})。"
                  "既にOllamaアプリが起動中か、ポート競合の可能性があります。")
            return is_ollama_running()
    print("[ollama] 起動を確認できませんでした（クラウドLLMで継続）")
    return False



def ensure_ollama(verbose: bool = True) -> bool:
    """ollamaが起動していなければ起動を試みる。
    成功/既に起動でTrue、未インストールや失敗でFalse（例外は投げない）。
    一度試みたら同一プロセスでは再試行しない。"""
    if is_ollama_running():
        return True
    if _TRIED["done"]:
        return is_ollama_running()
    _TRIED["done"] = True
    try:
        return start_ollama()
    except Exception:
        return False
