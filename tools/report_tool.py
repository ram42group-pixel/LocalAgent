# -*- coding: utf-8 -*-
#tools/report_tool.py — 診断レポート生成（Markdown / PDF）
"""
ペンテスト結果をレポートにまとめる。攻撃グラフ(engagement)の内容を自動で取り込み、
Markdownを生成し、可能ならPDFにも変換する。日本語対応。
"""
from __future__ import annotations

import os

from tools.base import Tool


def _workspace() -> str:
    import executor
    os.makedirs(executor.WORKSPACE, exist_ok=True)
    return executor.WORKSPACE


class ReportTool(Tool):
    name = "report"
    description = ("診断レポートを生成する。攻撃グラフの発見を自動で取り込み、Markdownを作成し、"
                   "format=pdf ならPDFにも変換する。本文は日本語で渡すこと")
    args = {"title": "レポートのタイトル",
            "body": "レポート本文（Markdown形式・日本語）。省略時は攻撃グラフから自動生成",
            "format": "md または pdf（既定md）",
            "filename": "出力ファイル名（拡張子なし・既定report）",
            "include_inventory": "true なら攻撃グラフの内容を末尾に自動追記（既定true）"}

    def run(self, args: dict) -> str:
        title = str(args.get("title", "ペネトレーションテスト診断レポート")).strip()
        body = str(args.get("body", "")).strip()
        fmt = str(args.get("format", "md")).strip().lower()
        fname = str(args.get("filename", "report")).strip() or "report"
        fname = os.path.basename(fname)
        include_inv = args.get("include_inventory", True)

        md = [f"# {title}", ""]
        if body:
            md.append(body)
        if include_inv:
            md.append("\n" + self._inventory_md())
        md_text = "\n".join(md)

        ws = _workspace()
        md_path = os.path.join(ws, fname + ".md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)
        outputs = [f"workspace/{fname}.md"]

        if fmt == "pdf":
            pdf_path = os.path.join(ws, fname + ".pdf")
            ok, msg = self._to_pdf(md_text, title, pdf_path)
            if ok:
                outputs.insert(0, f"workspace/{fname}.pdf")
            else:
                return (f"Markdownは生成しました（workspace/{fname}.md）が、"
                        f"PDF変換に失敗: {msg}")
        return "レポート生成完了: " + ", ".join(outputs)

    def _inventory_md(self) -> str:
        try:
            import engagement
            with engagement.Engagement() as e:
                st = e.full_state()
                paths = e.attack_paths()
        except Exception:
            return ""
        s = st["summary"]
        lines = ["## 診断サマリ", "",
                 f"- 発見ホスト数: {s['hosts']}",
                 f"- 公開サービス数: {s['services']}",
                 f"- 検出脆弱性数: {s['vulns']}（うち検証済み {s['verified_vulns']}）",
                 f"- 取得認証情報数: {s['credentials']}",
                 f"- その他の発見: {s['findings']}", ""]
        if st["hosts"]:
            lines.append("## ホスト別の詳細\n")
            for h in st["hosts"]:
                lines.append(f"### {h['ip']} {('('+h['hostname']+')') if h['hostname'] else ''}")
                lines.append(f"- OS: {h['os'] or '不明'}　状態: {h['status']}")
                if h["services"]:
                    lines.append("- 公開サービス:")
                    for sv in h["services"]:
                        prod = f"{sv['product']} {sv['version']}".strip()
                        lines.append(f"  - {sv['port']}/{sv['proto']} {sv['service'] or '?'} {prod}")
                if h["vulns"]:
                    lines.append("- 脆弱性:")
                    for v in h["vulns"]:
                        mark = "検証済" if v["verified"] else ("exploit有" if v["exploit_available"] else "未検証")
                        lines.append(f"  - **{v['cve'] or v['title']}** [{v['severity']} CVSS:{v['cvss']}] ({mark})")
                        if v["notes"]:
                            lines.append(f"    - {v['notes']}")
                if h["findings"]:
                    lines.append("- その他の発見:")
                    for fd in h["findings"]:
                        lines.append(f"  - [{fd['category']}] {fd['title']}")
                lines.append("")
        if paths:
            lines.append("## 推奨される対策の優先順位\n")
            for p in paths:
                lines.append(f"- {p['recommend']}")
        return "\n".join(lines)

    def _to_pdf(self, md_text: str, title: str, pdf_path: str) -> tuple[bool, str]:
        # reportlabで日本語対応PDFを生成（Markdownを簡易整形）
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.units import mm
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            # 日本語フォント（CID）を登録。環境差に備え複数候補を順に試す。
            # 全滅時のみHelvetica（日本語は化けるため最終手段）。
            font_name = ""
            for cand in ("HeiseiKakuGo-W5", "HeiseiMin-W3",
                         "STSong-Light", "MS-Gothic"):
                try:
                    pdfmetrics.registerFont(UnicodeCIDFont(cand))
                    font_name = cand
                    break
                except Exception:
                    continue
            if not font_name:
                font_name = "Helvetica"   # 日本語は表示できない（フォント未導入）
            styles = getSampleStyleSheet()
            base = ParagraphStyle("jp", parent=styles["Normal"],
                                  fontName=font_name, fontSize=10, leading=15)
            h1 = ParagraphStyle("jph1", parent=base, fontSize=16, leading=22, spaceAfter=8)
            h2 = ParagraphStyle("jph2", parent=base, fontSize=13, leading=18,
                                spaceBefore=10, spaceAfter=6)
            doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                                    topMargin=18*mm, bottomMargin=18*mm,
                                    leftMargin=18*mm, rightMargin=18*mm)
            flow = []
            for line in md_text.split("\n"):
                line = line.rstrip()
                esc = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
                # 太字 **x** → <b>x</b>
                import re
                esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
                if line.startswith("# "):
                    flow.append(Paragraph(esc[2:], h1))
                elif line.startswith("## "):
                    flow.append(Paragraph(esc[3:], h2))
                elif line.startswith("### "):
                    flow.append(Paragraph("<b>" + esc[4:] + "</b>", base))
                elif line.strip() == "":
                    flow.append(Spacer(1, 5))
                else:
                    flow.append(Paragraph(esc, base))
            doc.build(flow)
            return True, ""
        except Exception as ex:
            return False, str(ex)
