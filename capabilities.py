# -*- coding: utf-8 -*-
#capabilities.py — モデル能力ベクトルの“生きた”ストア
"""
各モデル(provider/model)の能力を多次元ベクトルで保持し、
ベンチ・AgentBench・実runのテレメトリで継続的に更新する（使うほど精緻化）。

次元(traits): reasoning, planning, tool_usage, security, reflection, speed, refusal_rate
- 0.0〜1.0（refusal_rateのみ「低いほど良い」指標）
- 更新は指数移動平均(EMA)。観測回数が増えるほど安定。

設計の要:
- モデル名からの推定は行わない（model_assign.estimateの代替）。
- 全てのセンサー（model_bench / agentbench / 実run）がここに書き込む単一の真実源。
- 動的ルーティング(router.py)がここを読む。
"""
from __future__ import annotations

import json
import os
import threading

_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "capabilities.json")
_LOCK = threading.Lock()

TRAITS = ["reasoning", "planning", "tool_usage", "security",
          "reflection", "speed", "refusal_rate"]

# EMA係数（小さいほど過去重視で安定、大きいほど直近重視で機敏）
_ALPHA = 0.30


def _blank() -> dict:
    v = {t: 0.0 for t in TRAITS}
    v["refusal_rate"] = 0.0
    return v


def _load() -> dict:
    if os.path.exists(_FILE):
        try:
            with open(_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"models": {}}


def _save(data: dict) -> None:
    with open(_FILE, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def get_vector(key: str) -> dict:
    """provider/model の能力ベクトルを返す（未知なら空ベクトル）。"""
    data = _load()
    m = data["models"].get(key)
    return dict(m["traits"]) if m else _blank()


def get_confidence(key: str) -> dict:
    """各traitの信頼度（0..1、サンプルが多いほど高い）を返す。"""
    data = _load()
    m = data["models"].get(key)
    return dict(m.get("confidence", {})) if m else {}


def get_samples(key: str) -> dict:
    """各traitの観測サンプル数を返す。"""
    data = _load()
    m = data["models"].get(key)
    return dict(m.get("samples", {})) if m else {}


def overall_confidence(key: str) -> float:
    """モデル全体の信頼度（測定したtraitの平均信頼度）。"""
    conf = get_confidence(key)
    return round(sum(conf.values()) / len(conf), 3) if conf else 0.0


def all_vectors() -> dict:
    """{key: {traits, observations, last_updated}} を返す。"""
    return _load()["models"]


def observe(key: str, traits: dict, weight: float = 1.0,
            source: str = "") -> dict:
    """観測値で能力ベクトルをEMA更新する。
    traits: 観測された trait→値（一部だけでも可）。
    weight: 観測の信頼度（多問のbench=高、単発run=低 等）。
    返り値: 更新後のベクトル。"""
    with _LOCK:
        data = _load()
        rec = data["models"].setdefault(
            key, {"traits": _blank(), "observations": 0, "sources": {},
                  "samples": {}, "confidence": {}})
        rec.setdefault("samples", {})       # 旧データ互換
        rec.setdefault("confidence", {})
        cur = rec["traits"]
        # 観測回数が少ないうちは観測値を強めに反映（学習を早める）
        n = rec["observations"]
        a = _ALPHA if n >= 3 else max(_ALPHA, 1.0 / (n + 1))
        a = min(1.0, a * weight)
        for t, val in traits.items():
            if t not in TRAITS:
                continue
            try:
                val = float(val)
            except Exception:
                continue
            # 0..1にクランプ（bench/agentbenchの異常値で能力像が壊れるのを防ぐ）
            val = max(0.0, min(1.0, val))
            cur[t] = round((1 - a) * cur.get(t, 0.0) + a * val, 4)
            # その trait の観測サンプル数を加算
            rec["samples"][t] = rec["samples"].get(t, 0) + 1
            # 信頼度: サンプルが増えるほど1に漸近（飽和曲線）。重み付きで加算。
            sn = rec["samples"][t]
            rec["confidence"][t] = round(1.0 - 1.0 / (1.0 + 0.5 * sn), 3)
        rec["observations"] = n + 1
        if source:
            rec["sources"][source] = rec["sources"].get(source, 0) + 1
        import datetime
        rec["last_updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        _save(data)
        return dict(cur)


def observe_from_bench(bench_results: dict) -> None:
    """学科ベンチ結果から reasoning/security/speed/refusal を観測。
    bench_results: {key: {scores:{reason,code,security,speed}, avg_ms, total_refused, ...}}"""
    # 速度正規化のため全体のmsレンジを使う
    all_ms = [r.get("avg_ms", 0) for r in bench_results.values() if r.get("avg_ms")]
    mx = max(all_ms) if all_ms else 1
    for key, r in bench_results.items():
        sc = r.get("scores", {})
        # 総問題数で拒否率を概算（domainごと正確化は後段で）
        refused = r.get("total_refused", 0)
        # ざっくり総問題数（scoresのdomain数×平均的問題数の代用に refusals 合計を使う）
        ref_rate = min(1.0, refused / 14.0) if refused else 0.0
        speed = 1.0 - (r.get("avg_ms", 0) / mx) if mx else 0.5
        traits = {
            "reasoning": sc.get("reason", 0.0),
            "tool_usage": sc.get("code", 0.0),     # コード力はツール操作の素地
            "security": sc.get("security", 0.0),
            "speed": round(max(0.0, min(1.0, speed)), 3),
            "refusal_rate": round(ref_rate, 3),
        }
        observe(key, traits, weight=1.0, source="bench")


def observe_from_agentbench(key: str, rubric: dict) -> None:
    """AgentBenchのルーブリック採点から planning/tool_usage/reflection/security を観測。
    rubric: {planning, tool_usage, reflection, security, ...} 各0..1"""
    observe(key, rubric, weight=1.0, source="agentbench")


def observe_from_run(key: str, telemetry: dict) -> None:
    """実runのテレメトリから観測（単発なので弱め重み）。
    telemetry: {planning, tool_usage, reflection, security, success} 各0..1"""
    t = {k: v for k, v in telemetry.items() if k in TRAITS}
    observe(key, t, weight=0.5, source="run")


def score_for_needs(key: str, needs: dict, use_confidence: bool = True) -> float:
    """能力ベクトルを、要求trait重みで内積評価する（ルーティング用）。
    needs: {trait: weight}。refusal_rateは「低いほど良い」ので反転して加味。
    use_confidence: Trueなら低信頼度traitの寄与を割り引く（未測定で勝たない）。"""
    v = get_vector(key)
    conf = get_confidence(key) if use_confidence else {}
    s = 0.0
    wsum = 0.0
    for t, w in needs.items():
        # 信頼度で割引（測定サンプルが少ないtraitは控えめに評価）
        c = conf.get(t, 1.0) if use_confidence else 1.0
        if t == "refusal_rate":
            s += w * (1.0 - v.get("refusal_rate", 0.0)) * c
        else:
            s += w * v.get(t, 0.0) * c
        wsum += abs(w)
    return round(s / wsum, 4) if wsum else 0.0


def rank_for_needs(needs: dict, candidates: list[str] = None) -> list[tuple]:
    """要求に対して候補モデルを能力順に並べる。返り値: [(key, score), ...]"""
    keys = candidates or list(all_vectors().keys())
    ranked = [(k, score_for_needs(k, needs)) for k in keys]
    ranked.sort(key=lambda x: -x[1])
    return ranked


def reset():
    """全能力ベクトルを消去（テスト用）。"""
    with _LOCK:
        _save({"models": {}})
