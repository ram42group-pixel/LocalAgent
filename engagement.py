# -*- coding: utf-8 -*-
#engagement.py — 攻撃対象のインベントリDBと攻撃グラフ（ペンテストの構造化知識）
"""
ペンテスト中に発見したものを構造化して保持する：
  hosts        : 発見したホスト（IP/OS/状態）
  services     : ホスト上のサービス（port/proto/service/version/banner）
  vulns        : サービスに紐づく脆弱性（CVE/深刻度/exploit有無）
  credentials  : 取得した認証情報（user/secret/種別/対象）
  findings     : 任意の発見事項（権限昇格の手がかり等）
これらから「攻撃経路（attack path）」を導出する。
発見を点でなく繋がりで管理し、横展開・権限昇格の判断に使う。
engagement.db に永続化（標的ごとに作戦記憶を保持）。
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engagement.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip TEXT UNIQUE NOT NULL, hostname TEXT, os TEXT,
    status TEXT DEFAULT 'up', notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER NOT NULL, port INTEGER, proto TEXT DEFAULT 'tcp',
    service TEXT, product TEXT, version TEXT, banner TEXT,
    UNIQUE(host_id, port, proto),
    FOREIGN KEY(host_id) REFERENCES hosts(id)
);
CREATE TABLE IF NOT EXISTS vulns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER, service_id INTEGER, cve TEXT, title TEXT,
    severity TEXT, cvss REAL, exploit_available INTEGER DEFAULT 0,
    verified INTEGER DEFAULT 0, notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER, username TEXT, secret TEXT, kind TEXT,
    realm TEXT, valid INTEGER DEFAULT 1, source TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id INTEGER, category TEXT, title TEXT, detail TEXT,
    severity TEXT, created_at TEXT
);
"""


def _now():
    import datetime
    return datetime.datetime.now().isoformat()


class Engagement:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def close(self):
        self.conn.close()

    # ---- ホスト ----
    def add_host(self, ip: str, hostname: str = "", os_: str = "",
                 status: str = "up", notes: str = "") -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO hosts (ip, hostname, os, status, notes, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET "
                "hostname=COALESCE(NULLIF(excluded.hostname,''),hosts.hostname), "
                "os=COALESCE(NULLIF(excluded.os,''),hosts.os), "
                "status=excluded.status",
                (ip, hostname, os_, status, notes, _now()))
            row = self.conn.execute("SELECT id FROM hosts WHERE ip=?", (ip,)).fetchone()
            return row["id"]

    # ---- サービス ----
    def add_service(self, ip: str, port: int, service: str = "", product: str = "",
                    version: str = "", proto: str = "tcp", banner: str = "") -> int:
        host_id = self.add_host(ip)
        with self.conn:
            self.conn.execute(
                "INSERT INTO services (host_id,port,proto,service,product,version,banner) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(host_id,port,proto) DO UPDATE SET "
                "service=COALESCE(NULLIF(excluded.service,''),services.service), "
                "product=COALESCE(NULLIF(excluded.product,''),services.product), "
                "version=COALESCE(NULLIF(excluded.version,''),services.version), "
                "banner=COALESCE(NULLIF(excluded.banner,''),services.banner)",
                (host_id, port, proto, service, product, version, banner))
            row = self.conn.execute(
                "SELECT id FROM services WHERE host_id=? AND port=? AND proto=?",
                (host_id, port, proto)).fetchone()
            return row["id"]

    # ---- 脆弱性 ----
    def add_vuln(self, ip: str, cve: str = "", title: str = "", severity: str = "",
                 cvss: float = 0.0, exploit_available: bool = False,
                 verified: bool = False, port: int = None, notes: str = "") -> int:
        host_id = self.add_host(ip)
        service_id = None
        if port is not None:
            row = self.conn.execute(
                "SELECT id FROM services WHERE host_id=? AND port=?",
                (host_id, port)).fetchone()
            service_id = row["id"] if row else None
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO vulns (host_id,service_id,cve,title,severity,cvss,"
                "exploit_available,verified,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (host_id, service_id, cve, title, severity, cvss,
                 int(exploit_available), int(verified), notes, _now()))
            return cur.lastrowid

    def mark_vuln_verified(self, vuln_id: int, verified: bool = True) -> None:
        with self.conn:
            self.conn.execute("UPDATE vulns SET verified=? WHERE id=?",
                              (int(verified), vuln_id))

    # ---- 認証情報 ----
    def add_credential(self, username: str, secret: str = "", kind: str = "password",
                       ip: str = "", realm: str = "", source: str = "") -> int:
        host_id = self.add_host(ip) if ip else None
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO credentials (host_id,username,secret,kind,realm,source,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (host_id, username, secret, kind, realm, source, _now()))
            return cur.lastrowid

    # ---- 発見事項 ----
    def add_finding(self, category: str, title: str, detail: str = "",
                    severity: str = "info", ip: str = "") -> int:
        host_id = self.add_host(ip) if ip else None
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO findings (host_id,category,title,detail,severity,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (host_id, category, title, detail, severity, _now()))
            return cur.lastrowid

    # ---- 参照 ----
    def summary(self) -> dict:
        c = self.conn
        return {
            "hosts": c.execute("SELECT COUNT(*) n FROM hosts").fetchone()["n"],
            "services": c.execute("SELECT COUNT(*) n FROM services").fetchone()["n"],
            "vulns": c.execute("SELECT COUNT(*) n FROM vulns").fetchone()["n"],
            "verified_vulns": c.execute(
                "SELECT COUNT(*) n FROM vulns WHERE verified=1").fetchone()["n"],
            "credentials": c.execute("SELECT COUNT(*) n FROM credentials").fetchone()["n"],
            "findings": c.execute("SELECT COUNT(*) n FROM findings").fetchone()["n"],
        }

    def full_state(self) -> dict:
        """攻撃グラフUI/プロンプト注入用：全インベントリを階層で返す。"""
        c = self.conn
        hosts = []
        for h in c.execute("SELECT * FROM hosts ORDER BY id").fetchall():
            svcs = [dict(s) for s in c.execute(
                "SELECT * FROM services WHERE host_id=? ORDER BY port", (h["id"],)).fetchall()]
            vulns = [dict(v) for v in c.execute(
                "SELECT * FROM vulns WHERE host_id=? ORDER BY cvss DESC", (h["id"],)).fetchall()]
            creds = [dict(cr) for cr in c.execute(
                "SELECT * FROM credentials WHERE host_id=?", (h["id"],)).fetchall()]
            finds = [dict(f) for f in c.execute(
                "SELECT * FROM findings WHERE host_id=?", (h["id"],)).fetchall()]
            hosts.append({**dict(h), "services": svcs, "vulns": vulns,
                          "credentials": creds, "findings": finds})
        # ホストに紐づかない認証情報・発見
        loose_creds = [dict(cr) for cr in c.execute(
            "SELECT * FROM credentials WHERE host_id IS NULL").fetchall()]
        return {"hosts": hosts, "loose_credentials": loose_creds,
                "summary": self.summary()}

    def attack_paths(self) -> list[dict]:
        """発見から攻撃経路の候補を導出する（単純なルールベース推論）。
        - exploit可能な脆弱性 → 初期侵入候補
        - 取得済みcred → 横展開/認証突破候補
        - 高CVSS未検証 → 優先検証候補
        """
        c = self.conn
        paths = []
        # 1) exploit可能な脆弱性＝初期アクセス経路
        for v in c.execute(
            "SELECT v.*, h.ip FROM vulns v JOIN hosts h ON v.host_id=h.id "
            "WHERE v.exploit_available=1 ORDER BY v.cvss DESC").fetchall():
            paths.append({
                "type": "initial_access",
                "target": v["ip"],
                "via": v["cve"] or v["title"],
                "severity": v["severity"], "cvss": v["cvss"],
                "verified": bool(v["verified"]),
                "recommend": f"{v['ip']} の {v['cve'] or v['title']} はexploit可能。"
                             + ("検証済み。権限昇格/横展開へ。" if v["verified"]
                                else "metasploitで検証を推奨。")})
        # 2) 取得済み認証情報＝横展開経路
        for cr in c.execute("SELECT * FROM credentials WHERE valid=1").fetchall():
            paths.append({
                "type": "lateral_movement",
                "target": "全ホスト",
                "via": f"{cr['username']} の{cr['kind']}",
                "recommend": f"取得した {cr['username']} の認証情報を crackmapexec 等で"
                             "他ホストに使い回して横展開を試す。"})
        # 3) 高CVSS未検証＝優先検証
        for v in c.execute(
            "SELECT v.*, h.ip FROM vulns v JOIN hosts h ON v.host_id=h.id "
            "WHERE v.verified=0 AND v.cvss>=7.0 ORDER BY v.cvss DESC LIMIT 5").fetchall():
            paths.append({
                "type": "priority_verify",
                "target": v["ip"], "via": v["cve"] or v["title"],
                "cvss": v["cvss"],
                "recommend": f"{v['ip']} の {v['cve']}（CVSS {v['cvss']}）は高深刻度・未検証。"
                             "優先的に検証すべき。"})
        return paths

    def prompt_text(self) -> str:
        """エージェントのプロンプトに注入する現状サマリ。"""
        s = self.summary()
        if s["hosts"] == 0:
            return ""
        lines = [f"【攻撃対象インベントリ】ホスト{s['hosts']} サービス{s['services']} "
                 f"脆弱性{s['vulns']}(検証済{s['verified_vulns']}) "
                 f"認証情報{s['credentials']} 発見{s['findings']}"]
        st = self.full_state()
        for h in st["hosts"][:8]:
            svc = ", ".join(f"{x['port']}/{x['service'] or '?'}"
                            + (f" {x['product']} {x['version']}".rstrip()
                               if x['product'] else "") for x in h["services"][:8])
            lines.append(f"- {h['ip']} ({h['os'] or 'OS不明'}): {svc or 'サービス未列挙'}")
            for v in h["vulns"][:3]:
                mark = "✓検証済" if v["verified"] else ("⚡exploit有" if v["exploit_available"] else "")
                lines.append(f"    ! {v['cve'] or v['title']} [{v['severity']} {v['cvss']}] {mark}")
        paths = self.attack_paths()
        if paths:
            lines.append("【推奨される次の攻撃経路】")
            for p in paths[:5]:
                lines.append(f"  → {p['recommend']}")
        return "\n".join(lines)

    def clear(self) -> None:
        with self.conn:
            for t in ("findings", "credentials", "vulns", "services", "hosts"):
                self.conn.execute(f"DELETE FROM {t}")
