# -*- coding: utf-8 -*-
#skill_system.py — スキルの昇格と再利用ゲート
"""
成功パターン(skill)を「使われた回数」と「成功率」で昇格させる。
昇格段階:
  candidate : 生成直後（未検証。試行回数が少ない）
  verified  : 数回使われ、成功率が一定以上（信頼できる）
  trusted   : 多数回使われ高成功率（planner が最優先で再利用）

planner はこのstageを見て、trusted/verified を優先提示し、
candidate は控えめに扱う（未検証スキルの暴走を防ぐ）。

LTM.skills の uses / success(累積成功数) から stage を算出する。
"""
from __future__ import annotations

# 昇格しきい値（uses=試行回数, rate=成功率）
_VERIFIED = {"uses": 2, "rate": 0.5}
_TRUSTED = {"uses": 5, "rate": 0.7}


def stage(uses: int, success: int) -> str:
    """試行回数と累積成功数から昇格段階を返す。"""
    if uses <= 0:
        return "candidate"
    rate = success / uses
    if uses >= _TRUSTED["uses"] and rate >= _TRUSTED["rate"]:
        return "trusted"
    if uses >= _VERIFIED["uses"] and rate >= _VERIFIED["rate"]:
        return "verified"
    return "candidate"


def rank_skills(skills: list[dict]) -> list[dict]:
    """スキル群に stage と優先度を付け、再利用に適した順へ並べる。
    skills: LTM.relevant_skills / all_skills の返り値（uses, success, similarity 含む）。"""
    _order = {"trusted": 3, "verified": 2, "candidate": 1}
    out = []
    for s in skills:
        uses = s.get("uses", 0)
        succ = s.get("success", 0)
        st = stage(uses, succ)
        s = dict(s)
        s["stage"] = st
        s["success_rate"] = round(succ / uses, 3) if uses else 0.0
        # 優先度 = 段階 × 類似度（planner提示順）
        s["priority"] = round(_order[st] * (0.5 + 0.5 * s.get("similarity", 0.5)), 3)
        out.append(s)
    out.sort(key=lambda x: -x["priority"])
    return out


def usable_skills(skills: list[dict], min_stage: str = "candidate") -> list[dict]:
    """再利用してよいスキルだけを返す（最低段階でフィルタ）。
    plannerが安全側に倒すとき min_stage='verified' 等を指定。"""
    _ord = {"candidate": 1, "verified": 2, "trusted": 3}
    th = _ord.get(min_stage, 1)
    ranked = rank_skills(skills)
    return [s for s in ranked if _ord[s["stage"]] >= th]


def format_for_planner(skills: list[dict]) -> str:
    """planner提示用の文字列。段階を明示し、trustedを推奨として強調。"""
    ranked = rank_skills(skills)
    if not ranked:
        return ""
    lines = []
    for s in ranked[:3]:
        badge = {"trusted": "★信頼", "verified": "○検証済",
                 "candidate": "△候補"}[s["stage"]]
        rate = f"成功率{int(s['success_rate'] * 100)}%" if s.get("uses") else "未試行"
        lines.append(f"{badge}「{s['name']}」({rate}): {' → '.join(s.get('steps', []))}")
    return "\n【再利用できるスキル（段階順）】" + " ／ ".join(lines)
