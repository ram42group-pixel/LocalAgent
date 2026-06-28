# -*- coding: utf-8 -*-
#phase_readiness.py — 取得情報の「量」と「質」でフェーズ移行を判断する
"""
固定回数（偵察N回で次へ）ではなく、攻撃グラフ(engagement)に蓄積された
情報の量と質を評価して、次のキルチェーン段階へ進むべきかを決める。

評価軸:
  量(quantity): ホスト/サービス/脆弱性/認証情報/発見事項の数
  質(quality) : サービスにversionが付いているか、脆弱性にCVE/CVSS/exploit有無が
               付いているか、攻撃経路(attack_paths)が導出できているか

「偵察は十分か（=攻撃に移れるか）」を readiness スコア(0..1)で返し、
閾値を超えたら「次段階へ進め」と判断する。これにより、
- 情報が薄いのに先走る/ 情報が十分なのに偵察を繰り返す の両方を防ぐ。
"""
from __future__ import annotations


def assess(summary: dict, attack_paths: list = None,
           quality: dict = None) -> dict:
    """攻撃グラフの集計から、フェーズ移行の準備度を評価する。
    summary: engagement.summary() の返り値
             {hosts, services, vulns, verified_vulns, credentials, findings}
    attack_paths: engagement.attack_paths()（導出された攻撃経路）
    quality: 任意。{services_with_version, vulns_with_cve} 等の質メトリクス。
    返り値: {readiness, recon_enough, can_exploit, reasons, next_phase}"""
    summary = summary or {}
    attack_paths = attack_paths or []
    quality = quality or {}

    hosts = summary.get("hosts", 0)
    services = summary.get("services", 0)
    vulns = summary.get("vulns", 0)
    verified = summary.get("verified_vulns", 0)
    creds = summary.get("credentials", 0)
    findings = summary.get("findings", 0)

    reasons = []

    # --- 量スコア（情報がどれだけ集まったか）---
    # サービスが1つも無ければ偵察はまだ不十分。
    qty = 0.0
    if hosts >= 1:
        qty += 0.2
    if services >= 1:
        qty += 0.3
    if services >= 3:
        qty += 0.1          # 複数サービスで攻撃面が広い
    if vulns >= 1:
        qty += 0.25
    if findings >= 1:
        qty += 0.15
    qty = min(1.0, qty)

    # --- 質スコア（集めた情報が攻撃に使える具体性を持つか）---
    qual = 0.0
    swv = quality.get("services_with_version", 0)
    vwc = quality.get("vulns_with_cve", 0)
    exploitable = quality.get("exploitable_vulns", 0)
    if services and swv:
        qual += 0.3 * min(1.0, swv / services)   # version判明率
    if vulns and vwc:
        qual += 0.3 * min(1.0, vwc / vulns)      # CVE紐付け率
    if exploitable >= 1:
        qual += 0.25                              # exploit可能な脆弱性がある
    if attack_paths:
        qual += 0.15                              # 攻撃経路が導出できている
    qual = min(1.0, qual)

    # --- 総合 readiness（量6:質4。質が無いと頭打ち）---
    readiness = round(0.6 * qty + 0.4 * qual, 3)

    # --- 判断 ---
    # 偵察十分: サービスを掴み、かつ量がある程度たまった
    recon_enough = services >= 1 and qty >= 0.6
    # 攻撃移行可: exploit可能な脆弱性 or 攻撃経路 or 認証情報がある（=次に打つ手がある）
    can_exploit = bool(exploitable >= 1 or attack_paths or creds >= 1
                       or (vulns >= 1 and vwc >= 1))

    if not services:
        reasons.append("サービス未発見。偵察を継続。")
    elif not recon_enough:
        reasons.append(f"情報量が不足(量={qty:.2f})。偵察を継続。")
    elif can_exploit:
        reasons.append("攻撃に使える具体的情報あり。攻撃段階へ移行可。")
    else:
        reasons.append("偵察は十分だが攻撃の足がかりが薄い。"
                       "version特定/CVE紐付け/exploit探索で質を上げる。")

    # 次に進むべき段階の示唆
    if not recon_enough:
        next_phase = "recon"
    elif can_exploit:
        next_phase = "exploit"
    else:
        next_phase = "weaponize"   # 偵察十分だが質不足→武器化(version/CVE特定)へ

    return {
        "readiness": readiness,
        "quantity": round(qty, 3),
        "quality": round(qual, 3),
        "recon_enough": recon_enough,
        "can_exploit": can_exploit,
        "next_phase": next_phase,
        "reasons": "; ".join(reasons),
    }


def quality_metrics(full_state: dict) -> dict:
    """engagement.full_state() から質メトリクスを抽出する。
    full_state は {hosts:[{services:[...], vulns:[...]}, ...]} の階層構造なので、
    各ホスト配下の services/vulns を集約して評価する。"""
    services, vulns = [], []
    for h in (full_state.get("hosts") or []):
        services.extend(h.get("services", []) or [])
        vulns.extend(h.get("vulns", []) or [])
    # 念のため top-level にもあれば拾う（後方互換）
    services.extend(full_state.get("services", []) or [])
    vulns.extend(full_state.get("vulns", []) or [])
    swv = sum(1 for s in services if (s.get("version") or "").strip())
    vwc = sum(1 for v in vulns if (v.get("cve") or "").strip())
    exploitable = sum(1 for v in vulns if v.get("exploit_available"))
    return {
        "services_with_version": swv,
        "vulns_with_cve": vwc,
        "exploitable_vulns": exploitable,
    }


def feedback_for(assessment: dict) -> str:
    """評価結果から、plannerへ返す誘導フィードバック文を作る。"""
    nxt = assessment.get("next_phase")
    if nxt == "recon":
        return ("偵察で得た情報がまだ薄い（攻撃に使える具体性が不足）。"
                "サービスのversion特定（nmap -sV/banner grab）や、エンドポイント"
                "列挙（gobuster/ffuf）で攻撃面を具体化すること。")
    if nxt == "weaponize":
        return ("偵察は十分。ただし攻撃の足がかりが薄い。"
                "searchsploit/cve_lookup で発見済みサービスを既知脆弱性に紐付け、"
                "exploit可能なベクトルを特定して武器化すること。")
    # exploit
    return ("攻撃に使える情報が揃った。偵察スキャンの繰り返しはやめ、"
            "exploit_run/sqlmap/commix 等で実際に攻撃・実証する段階へ進むこと。")
