# -*- coding: utf-8 -*-
#shell.py — 実行OSを判定し、適切なシェル/ランナーを提供する
"""
Windows なら PowerShell、Linux/macOS なら bash（無ければsh）を使う。
コード実行・コマンド実行・インストールはここで決めたシェルに委譲する。
隔離環境前提なので制限はかけない。
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys

SYSTEM = platform.system()        # "Windows" / "Linux" / "Darwin"
IS_WINDOWS = SYSTEM == "Windows"


def no_window_kwargs() -> dict:
    """subprocess に渡すと、Windowsでコンソール窓を出さずに実行できる kwargs を返す。
    Linux/macOS では空dict。各コマンド実行で **shell.no_window_kwargs() を展開して使う。
    （これを付けないと、WindowsでコマンドのたびにPowerShell窓が一瞬出る）"""
    if not IS_WINDOWS:
        return {}
    kw = {}
    try:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0   # SW_HIDE
        kw["startupinfo"] = si
    except Exception:
        pass
    return kw


def _first_available(cmds: list[list[str]], fallback: list[str]) -> list[str]:
    for c in cmds:
        if shutil.which(c[0]):
            return c
    return fallback


def shell_prefix() -> list[str]:
    """シェルコマンド文字列を実行するための前置き。'<prefix> + [コマンド文字列]'で使う。"""
    if IS_WINDOWS:
        # PowerShell優先。無ければ cmd
        return _first_available(
            [["pwsh", "-NoProfile", "-Command"], ["powershell", "-NoProfile", "-Command"]],
            ["cmd", "/c"],
        )
    # Linux/macOS: bash優先（Kali等もbash）、無ければsh
    return _first_available([["bash", "-lc"], ["sh", "-c"]], ["sh", "-c"])


def shell_name() -> str:
    p = shell_prefix()
    return p[0]


# code の language → (実行コマンド, ファイル拡張子)
def code_runner(language: str):
    lang = (language or "").lower()
    table = {
        "python": ([sys.executable], ".py"),
        "py": ([sys.executable], ".py"),
        "bash": (["bash"], ".sh"),
        "sh": (["sh"], ".sh"),
        "powershell": (shell_prefix()[:1] + ["-NoProfile", "-File"] if IS_WINDOWS else None, ".ps1"),
        "ps1": (shell_prefix()[:1] + ["-NoProfile", "-File"] if IS_WINDOWS else None, ".ps1"),
        "javascript": (["node"], ".js"),
        "js": (["node"], ".js"),
        "ruby": (["ruby"], ".rb"),
    }
    runner, suffix = table.get(lang, (None, ".txt"))
    return runner, suffix


def describe() -> str:
    """system.txt等へ渡す、現在の実行環境の説明。"""
    return f"OS={SYSTEM} / シェル={shell_name()}"


if __name__ == "__main__":
    print(describe())
    print("shell_prefix:", shell_prefix())
    print("python runner:", code_runner("python"))
