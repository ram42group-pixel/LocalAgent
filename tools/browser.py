# -*- coding: utf-8 -*-
#tools/browser.py — ブラウザ操作・スクリーンショット・画面解析ツール（Playwright）
"""
- BrowserTool: URLを開く/フォーム入力/クリック/スクショ。動的Web診断に使う。
- VisionTool: 画像（スクショ等）をマルチモーダルLLMで解析する。
Playwright が無ければ導入方法を案内する（pip install playwright && playwright install chromium）。
スクショはローカルの workspace に保存される。
"""
from __future__ import annotations

import os

from tools.base import Tool


def _workspace() -> str:
    import executor
    os.makedirs(executor.WORKSPACE, exist_ok=True)
    return executor.WORKSPACE


def _have_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class BrowserTool(Tool):
    name = "browser"
    description = ("ブラウザでWebページを開いて操作する（動的Web診断用）。"
                  "actions: goto/screenshot/fill/click/content/eval を順に実行")
    args = {
        "url": "最初に開くURL",
        "actions": ("操作の配列(任意)。各要素は "
                    '{"do":"fill","selector":"#user","value":"admin"} / '
                    '{"do":"click","selector":"#submit"} / '
                    '{"do":"screenshot","name":"after.png"} / '
                    '{"do":"content"} / {"do":"eval","script":"..."} 等'),
        "screenshot": "最後に全画面スクショを撮るか(既定true)",
    }

    def run(self, args: dict) -> str:
        if not _have_playwright():
            return ("エラー: Playwright未導入。次で導入してください:\n"
                    "  pip install playwright\n  playwright install chromium")
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return "エラー: http(s)のURLを指定してください"
        actions = args.get("actions", []) or []
        do_shot = args.get("screenshot", True)
        out = [f"== ブラウザ操作: {url} =="]
        try:
            from playwright.sync_api import sync_playwright
        except Exception as ex:
            return f"エラー: Playwright読込失敗: {ex}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True,
                                            args=["--no-sandbox"])
                page = browser.new_page(ignore_https_errors=True)
                page.goto(url, timeout=30000, wait_until="domcontentloaded")
                out.append(f"タイトル: {page.title()}")
                out.append(f"最終URL: {page.url}")
                for a in actions:
                    out.append(self._do_action(page, a))
                if do_shot:
                    path = os.path.join(_workspace(), "browser_shot.png")
                    page.screenshot(path=path, full_page=True)
                    out.append(f"スクショ保存: workspace/browser_shot.png")
                browser.close()
        except Exception as ex:
            out.append(f"エラー: {ex}")
        return "\n".join(out)

    def _do_action(self, page, a: dict) -> str:
        do = a.get("do", "")
        sel = a.get("selector", "")
        try:
            if do == "fill":
                if not sel:
                    return "  操作失敗(fill): selector が必要です"
                page.fill(sel, a.get("value", ""), timeout=10000)
                return f"  入力: {sel} ← (値)"
            if do == "click":
                if not sel:
                    return "  操作失敗(click): selector が必要です"
                page.click(sel, timeout=10000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                return f"  クリック: {sel} → {page.url}"
            if do == "screenshot":
                name = a.get("name", "shot.png")
                path = os.path.join(_workspace(), "browser_" + os.path.basename(name))
                page.screenshot(path=path, full_page=True)
                return f"  スクショ: workspace/{os.path.basename(path)}"
            if do == "content":
                html = page.content()
                return f"  本文(先頭800字):\n{html[:800]}"
            if do == "eval":
                res = page.evaluate(a.get("script", ""))
                return f"  eval結果: {str(res)[:300]}"
            if do == "wait":
                page.wait_for_timeout(int(a.get("ms", 1000)))
                return "  待機"
            return f"  未対応の操作: {do}"
        except Exception as ex:
            return f"  操作失敗({do}): {ex}"


class VisionTool(Tool):
    name = "vision"
    description = ("画像（スクリーンショット等）をAIで解析する。"
                  "Webの画面に何が映っているか・ログイン画面か・管理画面の露出等を判断")
    args = {"image": "解析する画像パス（workspace相対。例: browser_shot.png）",
            "question": "画像について聞きたいこと（例: ログインフォームや管理画面はあるか）"}

    def run(self, args: dict) -> str:
        image = str(args.get("image", "")).strip()
        question = str(args.get("question", "この画面に何が表示されているか説明して")).strip()
        if not image:
            return "エラー: image を指定してください"
        path = image if os.path.isabs(image) else os.path.join(_workspace(), image)
        if not os.path.exists(path):
            # browser_ プレフィックス付きも試す
            alt = os.path.join(_workspace(), "browser_" + os.path.basename(image))
            if os.path.exists(alt):
                path = alt
            else:
                return f"エラー: 画像が見つからない: {image}"
        try:
            import google_studio.google_studio_control as g
            return g.analyze_image(path, question)
        except Exception as ex:
            return (f"エラー: 画像解析に失敗（Google Studioのキー設定が必要）: {ex}")


class WebScanTool(Tool):
    name = "web_scan"
    description = ("Webページの攻撃対象面を受動的に収集する（フォーム/入力欄/リンク/"
                  "セキュリティヘッダ/Cookie属性）。診断の初手に使う。注入はしない")
    args = {"url": "診断対象URL",
            "login": ('任意。ログインが必要なら '
                      '{"user_sel":"#user","pass_sel":"#pass","user":"a","pass":"b","submit_sel":"#login"}')}

    def run(self, args: dict) -> str:
        if not _have_playwright():
            return ("エラー: Playwright未導入。pip install playwright && "
                    "playwright install chromium")
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return "エラー: http(s)のURLを指定してください"
        login = args.get("login")
        out = [f"== Web攻撃面スキャン: {url} =="]
        try:
            from playwright.sync_api import sync_playwright
        except Exception as ex:
            return f"エラー: Playwright読込失敗: {ex}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                ctx = browser.new_context(ignore_https_errors=True)
                page = ctx.new_page()
                # セキュリティヘッダを拾うためレスポンスを監視
                headers_seen = {}

                def _on_resp(resp):
                    if resp.url.rstrip("/") == url.rstrip("/"):
                        headers_seen.update(resp.headers)
                page.on("response", _on_resp)
                page.goto(url, timeout=30000, wait_until="domcontentloaded")

                # 任意ログイン
                if isinstance(login, dict) and login.get("user_sel"):
                    try:
                        page.fill(login["user_sel"], login.get("user", ""), timeout=8000)
                        page.fill(login["pass_sel"], login.get("pass", ""), timeout=8000)
                        page.click(login.get("submit_sel", "button[type=submit]"), timeout=8000)
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
                        out.append(f"ログイン試行後URL: {page.url}")
                    except Exception as ex:
                        out.append(f"ログイン試行失敗: {ex}")

                # フォームと入力欄
                forms = page.evaluate("""() => {
                    return [...document.forms].map(f => ({
                        action: f.action, method: f.method,
                        inputs: [...f.elements].map(e => ({
                            name: e.name, type: e.type, id: e.id
                        })).filter(e => e.name || e.id)
                    }));
                }""")
                out.append(f"\n[フォーム] {len(forms)}個")
                for i, f in enumerate(forms[:8], 1):
                    fields = ", ".join(
                        f"{e['name'] or e['id']}({e['type']})" for e in f["inputs"][:10])
                    out.append(f"  {i}. {f['method'].upper()} {f['action']}\n     入力: {fields}")

                # リンク（同一ホスト中心）
                links = page.evaluate("""() => [...document.querySelectorAll('a[href]')]
                    .map(a => a.href).slice(0, 200)""")
                uniq = sorted(set(links))
                out.append(f"\n[リンク] {len(uniq)}個（抜粋）")
                for l in uniq[:15]:
                    out.append(f"  {l}")

                # セキュリティヘッダの評価
                out.append("\n[セキュリティヘッダ]")
                checks = {
                    "content-security-policy": "CSP",
                    "strict-transport-security": "HSTS",
                    "x-frame-options": "クリックジャッキング対策",
                    "x-content-type-options": "MIMEスニッフィング対策",
                    "referrer-policy": "Referrerポリシー",
                }
                low = {k.lower(): v for k, v in headers_seen.items()}
                for h, label in checks.items():
                    mark = "✓ あり" if h in low else "✗ 欠落"
                    out.append(f"  {mark}: {label} ({h})")
                server = low.get("server", "")
                if server:
                    out.append(f"  Server: {server}（バージョン露出に注意。cve_lookup推奨）")

                # Cookie属性
                cookies = ctx.cookies()
                if cookies:
                    out.append(f"\n[Cookie] {len(cookies)}個")
                    for c in cookies[:8]:
                        flags = []
                        if not c.get("httpOnly"):
                            flags.append("HttpOnly欠落")
                        if not c.get("secure"):
                            flags.append("Secure欠落")
                        ss = c.get("sameSite", "")
                        if ss in ("None", ""):
                            flags.append("SameSite弱い")
                        out.append(f"  {c['name']}: " +
                                   ("⚠ " + ", ".join(flags) if flags else "属性OK"))

                browser.close()
                out.append("\n→ 次の手: フォームの入力欄に対し、必要なら sqlmap 等で"
                           "個別に検証する（破壊的検証の前は assist で確認）。")
        except Exception as ex:
            out.append(f"エラー: {ex}")
        return "\n".join(out)


class WebInspectTool(Tool):
    name = "web_inspect"
    description = ("URLを開いてスクショを撮り、その画面をAIで解析して結果を返す"
                  "（browser+visionを1手で連結）。画面を見て次の手を決めたい時に使う")
    args = {"url": "開くURL",
            "question": "画面について判断したいこと（既定: 画面の内容と気になる点）",
            "actions": "任意。開いた後の操作配列（browserと同形式）"}

    def run(self, args: dict) -> str:
        url = str(args.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return "エラー: http(s)のURLを指定してください"
        question = str(args.get("question", "")).strip() or \
            "この画面に何が表示されているか、ログイン/管理画面や情報露出など気になる点を述べて"
        # 1) browserで開いて操作＋スクショ
        browser = BrowserTool()
        bres = browser.run({"url": url, "actions": args.get("actions", []),
                            "screenshot": True})
        # 2) visionでスクショを解析
        vision = VisionTool()
        vres = vision.run({"image": "browser_shot.png", "question": question})
        return f"{bres}\n\n[画面解析]\n{vres}"
