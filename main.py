# -*- coding: utf-8 -*-
import sys
# Windowsのコンソール既定(cp932)だと日本語print出力が文字化けするため、
# 標準出力/エラーをUTF-8に再設定する（Python3.7+。失敗しても続行）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
import ollamas.ollama_server as ollama_server
import ollamas.ollama_control as ollama_control
import fast_connect
if not ollama_server.ensure_ollama():
    sys.exit(0)

models = ollama_control.get_models()
response = ollama_control.send(
    text=fast_connect.load_prompt("system"),
    model=models[0]
)
print("モデル :", response.model)
print("日時   :", response.created_at)
print("ロール :", response.role)
print("完了   :", response.done)
print("本文   :", response.content)
