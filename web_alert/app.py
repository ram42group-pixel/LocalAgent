# -*- coding: utf-8 -*-
#web_alert/app.py — 脆弱性練習アプリ（ローカル専用・各脆弱性3レベル/激難）
"""
意図的に脆弱性を含む教材アプリ。各脆弱性を level=1/2/3 で難化（L3=激難）。
攻略するとレベル別フラグが得られる。

⚠️ 127.0.0.1 のみにバインド。インターネットに公開しないこと。
⚠️ コマンドインジェクションは実行を *模擬* するのみ（実シェル実行なし）。
レベル設計: L1=フィルタなし / L2=素朴なフィルタ / L3=複合フィルタ(激難・高度な回避が必要)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3

from flask import Flask, request, render_template_string

app = Flask(__name__)
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "practice.db")

# レベル別フラグ（攻略で得られる）
def F(vuln, lv):
    return f"FLAG{{{vuln}_L{lv}_" + hashlib.md5(f"{vuln}{lv}".encode()).hexdigest()[:8] + "}"

ADMIN_PW = "s3cr3t_admin_pw"
COOKIE_SALT = "wa_salt_2026"   # L3で使う弱い署名のソルト（ヒントで露出）


def init_db():
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password TEXT);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, token TEXT, owner TEXT, body TEXT);
        INSERT INTO users (username,password) VALUES ('admin','{ADMIN_PW}');
        INSERT INTO users (username,password) VALUES ('alice','alicepw');
        INSERT INTO notes (token,owner,body) VALUES ('1','alice','aliceのメモ');
        INSERT INTO notes (token,owner,body) VALUES ('2','admin','管理者メモ: {F("idor",1)}');
        INSERT INTO notes (token,owner,body) VALUES
            ('{hashlib.md5(b"note-admin-L3").hexdigest()}','admin','L3メモ: {F("idor",3)}');
    """)
    con.commit()
    con.close()


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>脆弱性練習</title>
<style>body{font-family:sans-serif;background:#0f1116;color:#e8edf2;max-width:780px;margin:24px auto;padding:0 16px}
a{color:#6fb3ff}h1{font-size:20px}h2{font-size:15px;color:#7bd8a8}.card{background:#171d24;border:1px solid #2a333e;border-radius:10px;padding:14px;margin:10px 0}
input,button,select{padding:6px 8px;border-radius:6px;border:1px solid #2a333e;background:#0f1116;color:#e8edf2}
.note{color:#ff8a6f;font-size:13px}pre{background:#0b0d12;padding:8px;border-radius:6px;overflow:auto}
.lv{color:#c9a8ff;font-family:monospace}</style></head><body>
<h1>⚠️ 脆弱性練習アプリ（ローカル専用）</h1>
<p class="note">意図的に脆弱です。各脆弱性は level=1/2/3 で難化（L3=激難）。公開禁止。</p>
{{ body|safe }}<hr><p><a href="/">トップ</a></p></body></html>"""


def page(b):
    return render_template_string(PAGE, body=b)


def lv():
    try:
        return max(1, min(3, int(request.args.get("level", "1"))))
    except Exception:
        return 1


def levelbar(path):
    return (f'<p class="lv">難易度: '
            + ' | '.join(f'<a href="{path}?level={i}">L{i}{"(激難)" if i==3 else ""}</a>'
                         for i in (1, 2, 3)) + '</p>')


@app.route("/")
def index():
    items = [("ログイン(SQLi)", "/login"), ("検索(XSS)", "/search?q=test"),
             ("ファイル閲覧(パストラバーサル)", "/view?file=welcome.txt"),
             ("疎通確認(コマンドインジェクション)", "/ping?host=127.0.0.1"),
             ("メモ閲覧(IDOR)", "/note?id=1"), ("管理者(安全でないCookie)", "/admin")]
    li = "".join(f'<li><a href="{u}">{n}</a></li>' for n, u in items)
    return page(f'<div class="card"><h2>機能一覧（各 ?level=1/2/3）</h2><ul>{li}</ul></div>')


# ===== 1. SQLi =====
@app.route("/login", methods=["GET", "POST"])
def login():
    L = lv()
    msg = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        fu = u
        # レベル別フィルタ
        if L >= 2:
            fu = fu.replace(" ", "")                      # 空白除去 → /**/ で回避
        if L >= 3:
            fu = re.sub(r"--|#", "", fu)                  # 行コメント除去 → インラインコメント /**/ + OR で回避
        con = sqlite3.connect(DB)
        q = f"SELECT username,password FROM users WHERE username='{fu}' AND password='{p}'"
        try:
            row = con.execute(q).fetchone()
        except Exception as e:
            row = None
            msg = f"<pre>SQLエラー: {e}</pre>"
        con.close()
        if row and row[0] == "admin":
            msg = (f"<p>admin認証情報が漏洩！</p><pre>username: admin\npassword: {row[1]}</pre>"
                   f"<p>答え: {row[1]}　{F('sqli', L)}</p>")
        elif row:
            msg = f"<p>ようこそ {row[0]}（adminを狙え）</p>"
        elif not msg:
            msg = "<p>認証失敗</p>"
    hints = {1: "admin' -- ", 2: "空白禁止 → admin'/**/--/**/x", 3: "空白と--禁止 → admin'/**/OR/**/'1'='1"}
    return page(levelbar("/login") + f"""<div class="card"><h2>ログイン (L{L})</h2>
    <form method="post"><input type="hidden" name="level" value="{L}">
    ユーザー名 <input name="username"><br><br>パスワード <input name="password"><br><br>
    <button>ログイン</button></form><p class="note">ヒント: {hints[L]}</p></div>{msg}""")


# ===== 2. XSS =====
@app.route("/search")
def search():
    L = lv()
    q = request.args.get("q", "")
    s = q
    if L >= 2:
        s = re.sub(r"(?i)<script.*?>.*?</script>|<script.*?>", "", s)   # scriptタグ除去 → onerror等で回避
    if L >= 3:
        s = re.sub(r"(?i)on\w+\s*=|javascript:|<script|</script|alert", "", s)  # 複合除去 → 難読化/別ベクタ
    # 出力に実行可能ベクタが残っていれば成立とみなす（実行は安全に検知のみ）
    exec_vector = bool(re.search(r"(?i)<script|on\w+\s*=|<svg|<img[^>]+on", s))
    flag = f"<p>XSS成立！ {F('xss', L)}</p>" if exec_vector else ""
    hints = {1: "<script>alert(1)</script>", 2: "scriptタグ禁止 → <img src=x onerror=alert(1)>",
             3: "on*/script/alert禁止 → <svg/OnLoAd=confirm`1`> 等の難読化"}
    return page(levelbar("/search") + f"""<div class="card"><h2>検索 (L{L})</h2>
    <form><input type="hidden" name="level" value="{L}"><input name="q" value="{s}"><button>検索</button></form>
    <p>結果: {s}</p>{flag}<p class="note">ヒント: {hints[L]}</p></div>""")


# ===== 3. パストラバーサル（安全に模擬・実ファイルは開かない）=====
@app.route("/view")
def view():
    L = lv()
    fname = request.args.get("file", "welcome.txt")
    f = fname
    if L >= 2:
        f = f.replace("../", "")          # 1回だけ除去 → ....// で回避
    if L >= 3:
        while "../" in f:
            f = f.replace("../", "")      # 繰り返し除去 → URLエンコード %2e%2e%2f で回避
    # 正規化後の文字列に基づき「機密ファイルに到達したか」を判定（実ファイルは読まない）
    decoded = f.replace("%2e", ".").replace("%2f", "/").replace("%252e", ".")
    reached = ("secret" in decoded and (".." in decoded or "%2e%2e" in fname.lower())) \
              or "etc/passwd" in decoded
    if reached:
        content = f"root:x:0:0:root:/root:/bin/bash\n{F('traversal', L)}"
    elif "welcome" in f:
        content = "ようこそ。../ で上位へ辿れる？"
    else:
        content = "(ファイルなし)"
    hints = {1: "../secret_passwd.txt", 2: "../除去1回 → ....//secret_passwd.txt",
             3: "../除去ループ → 二重エンコード ..%252f..%252fsecret_passwd.txt"}
    return page(levelbar("/view") + f"""<div class="card"><h2>ファイル閲覧 (L{L})</h2>
    <form><input type="hidden" name="level" value="{L}"><input name="file" value="{fname}" size="44"><button>表示</button></form>
    <pre>{content}</pre><p class="note">ヒント: {hints[L]}</p></div>""")


# ===== 4. コマンドインジェクション（実行せず模擬）=====
@app.route("/ping")
def ping():
    L = lv()
    host = request.args.get("host", "127.0.0.1")
    h = host
    blocked = []
    if L >= 2:
        for t in [";", "&&"]:
            if t in h:
                h = h.replace(t, "")
                blocked.append(t)
    if L >= 3:
        for t in [";", "&&", "|", "&", " ", "\n"]:   # 空白も禁止 → ${IFS} と $()/`` で回避
            h = h.replace(t, "")
    out = f"PING {host} ... (模擬)"
    # 残存する注入ベクタを検知（実行はしない）
    vectors = ["|", "`", "$(", "${IFS}", "%0a", "\n"] if L >= 3 else [";", "&&", "||", "|", "`", "$("]
    if any(v in host for v in vectors):
        out += f"\n$ (注入コマンドが実行された想定)\nwww-data\n{F('cmdi', L)}"
    hints = {1: "127.0.0.1; whoami", 2: ";と&&禁止 → 127.0.0.1|whoami か `whoami`",
             3: "区切り・空白禁止 → 127.0.0.1`whoami` や $(cat${IFS}/etc/passwd)"}
    return page(levelbar("/ping") + f"""<div class="card"><h2>疎通確認 (L{L})</h2>
    <form><input type="hidden" name="level" value="{L}"><input name="host" value="{host}" size="44"><button>ping</button></form>
    <pre>{out}</pre><p class="note">ヒント: {hints[L]}（※実行はせず成立を模擬）</p></div>""")


# ===== 5. IDOR =====
@app.route("/note")
def note():
    L = lv()
    nid = request.args.get("id", "1")
    token = nid
    if L == 2:
        # base64化したtokenを要求 → base64('2') を渡す必要
        try:
            token = base64.b64decode(nid).decode()
        except Exception:
            token = nid
    if L == 3:
        # md5トークン直接指定（推測/列挙が必要）
        token = nid
    con = sqlite3.connect(DB)
    row = con.execute("SELECT owner,body FROM notes WHERE token=?", (token,)).fetchone()
    con.close()
    body = f"<p>所有者: {row[0]}</p><pre>{row[1]}</pre>" if row else "<p>なし</p>"
    hints = {1: "id=2 で管理者メモ", 2: "tokenはbase64 → id=" + base64.b64encode(b"2").decode(),
             3: "tokenはmd5('note-admin-L3') → id=" + hashlib.md5(b"note-admin-L3").hexdigest()}
    return page(levelbar("/note") + f"""<div class="card"><h2>メモ閲覧 (L{L})</h2>
    <form><input type="hidden" name="level" value="{L}"><input name="id" value="{nid}" size="40"><button>表示</button></form>
    {body}<p class="note">ヒント: {hints[L]}</p></div>""")


# ===== 6. 安全でないCookie =====
@app.route("/admin")
def admin():
    L = lv()
    c = request.cookies.get("role", "")
    ok = False
    if L == 1:
        try:
            ok = base64.b64decode(c).decode() == "admin"
        except Exception:
            pass
    elif L == 2:
        try:
            ok = json.loads(base64.b64decode(c).decode()).get("role") == "admin"
        except Exception:
            pass
    else:  # L3: role.署名（弱い: md5(role+salt)）。saltがヒントで露出 → 偽造可能
        try:
            role, sig = c.rsplit(".", 1)
            ok = (role == "admin" and
                  sig == hashlib.md5((role + COOKIE_SALT).encode()).hexdigest())
        except Exception:
            pass
    if ok:
        return page(levelbar("/admin") + f'<div class="card"><h2>管理者 (L{L})</h2><p>{F("cookie", L)}</p></div>')
    g2 = base64.b64encode(b'{"role":"admin"}').decode()
    sig3 = hashlib.md5(("admin" + COOKIE_SALT).encode()).hexdigest()
    hints = {1: "Cookie role=base64('admin')=" + base64.b64encode(b"admin").decode(),
             2: "role=base64(JSON) → " + g2,
             3: f"role=admin.<md5(role+'{COOKIE_SALT}')> → admin.{sig3}"}
    return page(levelbar("/admin") + f"""<div class="card"><h2>管理者 (L{L})</h2>
    <p>権限なし</p><p class="note">ヒント: {hints[L]}</p></div>""")


if __name__ == "__main__":
    init_db()
    print("=" * 56)
    print(" 脆弱性練習アプリ（各脆弱性3レベル / L3=激難）")
    print(" URL: http://127.0.0.1:5000/   ⚠️ 公開禁止・実行は模擬")
    print("=" * 56)
    app.run(host="127.0.0.1", port=5000, debug=False)
