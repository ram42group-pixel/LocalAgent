# -*- coding: utf-8 -*-
#web_app2.py — モデルテスト（ベンチマーク）専用サーバー
"""
本体(web_app.py)とは別エントリ。モデル評価・テストだけを行う軽量サーバー。
起動:  python web_app2.py   →  http://127.0.0.1:8771/

本体のハンドラ(web_app.H)をそのまま再利用し、ルート("/")だけ
ベンチマークページに差し替える。テスト結果（割り当て）は本体と同じ
experts.json / routes.json に書き込まれるため、本体側に反映される。
"""
from __future__ import annotations

from http.server import ThreadingHTTPServer

import web_app

HOST, PORT = "127.0.0.1", 8771


class BenchHandler(web_app.H):
    """ルート("/")だけベンチページにする以外は本体ハンドラと同じ。"""

    def do_GET(self):  # noqa: N802
        from urllib.parse import urlparse
        if urlparse(self.path).path == "/":
            try:
                with open("web/benchmark.html", "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                return self._send(500, {"error": str(e)})
        return super().do_GET()


def main():
    try:
        import ollamas.ollama_server as _ollama_srv
        _ollama_srv.ensure_ollama()
    except Exception:
        pass
    print(f"モデルテスト・サーバー起動: http://{HOST}:{PORT}/")
    print("（本体 web_app.py とは別。テスト結果は専門家設定に共有されます）")
    ThreadingHTTPServer((HOST, PORT), BenchHandler).serve_forever()


if __name__ == "__main__":
    main()
