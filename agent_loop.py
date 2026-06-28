# -*- coding: utf-8 -*-
#agent_loop.py — 本体ループ（goal分解→目的ごとに計画→実行→要約→記憶）
"""
LLMアクセスは providers.ask(role, messages) に統一。
進行状況は emit(event:dict) で外へ流す（CLIはコンソール表示、Webはブラウザへ）。
モデル差し替えは providers/registry.py の ROLE_ROUTES（またはWeb UI）。
"""
import sys as _sys
try:  # WindowsのコンソールをUTF-8に揃える（文字化け防止）
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

import sys

import fast_connect
import json_checker as jc
import executor
import shell
import ssh_session
import installs
import session
import modes
import stats
import budget
import kali_tools
from dedup import Deduplicator, explain_duplicate, OutputTracker, explain_bad_json
from providers import ask
from memory import LongTermMemory, ShortTermMemory
from memory.short_term import is_failure_result

RETRIES = 3
MAX_TURNS_PER_OBJECTIVE = 12
MAX_TURNS_PENTEST = 24       # ペンテストは多段（偵察〜エクスプロイト）で手数が要る
# 攻撃的モード（目的に実証/エクスプロイトを含める・手数増・KG記録・厳格judge対象）
_OFFENSIVE_MODES = ("pentest", "recon", "killchain")

# 実行時フック（run_agent が設定）。emit=進行イベント, approver/dry=実行制御
_HOOK = {"emit": None, "approver": None, "dry": False}
_LTM_REF = {"ltm": None}   # plan_next_action から知識グラフ参照用
_WORLD_REF = {"world": None}   # Phase2: 世界状態(WorldState)への参照
_PROV_REF = {"prov": None}     # Phase5: Decision Provenance レコーダ
_LOOP_REF = {"det": None}      # Phase5: Loop Detector
_APPLIED_RULES = {"ids": []}   # Phase2: 直近の計画で適用したルールID（成否帰属用）
_EXPLORE_REF = {"engine": None}    # Phase3: 探索エンジン（objective単位）
_STRATEGY_REF = {"engine": None}   # Phase3: 戦略エンジン（run単位）
_CHOSEN_HYP = {"hyp": None}        # Phase3: 今ターン選んだ仮説（結果帰属に同一キーを使う）
_TARGET = {"info": None, "ctx": None}   # 対象。ctx=不変のTarget Context(唯一の信頼源)


def _cleanup_run_refs():
    """run終了時（正常・異常問わず）に呼ぶ後始末。World State接続のリークを防ぐ。"""
    try:
        _w = _WORLD_REF.get("world")
        if _w is not None:
            _w.close()
    except Exception:
        pass
    # Phase5: Decision Provenance を確定・クローズ
    try:
        _p = _PROV_REF.get("prov")
        if _p is not None:
            _p.finish_run("done")
            _p.close()
    except Exception:
        pass
    _PROV_REF["prov"] = None
    _LOOP_REF["det"] = None
    _WORLD_REF["world"] = None
    _EXPLORE_REF["engine"] = None
    _STRATEGY_REF["engine"] = None
    _CHOSEN_HYP["hyp"] = None
    _TARGET["info"] = None
    _TARGET["ctx"] = None
_ROUTING = {"on": True}    # 動的ルーティング（能力ベクトルでモデル選択）のON/OFF
_USED_SKILLS = {"ids": set()}  # 今の目的で planner に提示されたスキルID（成否を後で記録）
# 実行中の介入キュー: ユーザーが軌道修正のメッセージを差し込める。
# 各ターンの冒頭で drain され、次のプランに feedback として注入される。
import threading as _threading
_INTERVENTIONS = {"queue": [], "lock": _threading.Lock(), "stop": False}


def push_intervention(text: str) -> None:
    """実行中のエージェントへ介入メッセージを送る（UI/APIから呼ぶ）。
    次のターンの冒頭で取り出され、プランへの指示として反映される。"""
    if not text:
        return
    with _INTERVENTIONS["lock"]:
        _INTERVENTIONS["queue"].append(str(text))


def request_stop() -> None:
    """実行中のエージェントを次の安全な区切りで停止させる。"""
    with _INTERVENTIONS["lock"]:
        _INTERVENTIONS["stop"] = True


def _drain_interventions() -> str:
    """溜まった介入メッセージを取り出して連結（キューは空にする）。"""
    with _INTERVENTIONS["lock"]:
        if not _INTERVENTIONS["queue"]:
            return ""
        msgs = _INTERVENTIONS["queue"][:]
        _INTERVENTIONS["queue"] = []
    return " ".join(msgs)


def _should_stop() -> bool:
    with _INTERVENTIONS["lock"]:
        return _INTERVENTIONS["stop"]


def _reset_interventions() -> None:
    """run開始時にキューと停止フラグを初期化（前回の残りを持ち越さない）。"""
    with _INTERVENTIONS["lock"]:
        _INTERVENTIONS["queue"] = []
        _INTERVENTIONS["stop"] = False
_STEPS = {"plan": []}     # 現在の目的の多段階プラン（手順リスト）
_STM_REF = {"stm": None}  # 現在の短期記憶（ビューア用）


def _console(e: dict) -> None:
    t = e.get("type")
    if t == "goal_done":
        print(f"\n[STEP1] ゴール: {e['goal']}")
        for i, o in enumerate(e["objectives"], 1):
            print(f"   目的{i}: {o}")
        print(f"   タグ: {e['tags']}")
    elif t == "objective_start":
        print(f"\n[STEP2] 目的({e['index']}/{e['total']}): {e['objective']}")
        for m in e.get("related", []):
            print(f"   関連記憶: {m}")
    elif t == "llm_response":
        print(f"   <{e['role']}@{e['model']}> {e['content'][:120]}")
    elif t == "parse_error":
        print(f"   ! 解析失敗({e['attempt']}): {e['error']}")
    elif t == "provider_refusal":
        print(f"   ⛔ LLMが生成を拒否した可能性: {e.get('hint','')}")
    elif t == "local_fallback":
        print(f"   🔁 ローカルLLM(ollama)で再実行しました（{e.get('role')}）")
    elif t == "skill_learned":
        if e.get("name"):
            print(f"   🎓 スキル習得: 「{e['name']}」 {' → '.join(e.get('steps',[]))}")
    elif t == "consolidated":
        print(f"   🧹 記憶整理: 重複{e.get('removed_duplicates',0)}件・教訓{e.get('removed_lessons',0)}件を削除")
    elif t == "debate":
        print(f"   💬 討論{e.get('round')}: {'合意' if e.get('agreed') else '差し戻し → '+e.get('advice','')}")
    elif t == "critic":
        print(f"   🧐 critic: {'OK' if e.get('ok') else '差し戻し → '+e.get('advice','')}")
    elif t == "critique":
        mark = "✅成功" if e.get("success") else "❌失敗"
        print(f"   🔎 批評: {mark}" + (f" / 原因: {e.get('cause','')}" if not e.get("success") else ""))
    elif t == "reflection":
        print(f"   🪞 振り返り: 失敗{e.get('failures',0)}・成功{e.get('successes',0)}"
              f" → 新ルール{e.get('new_rules',0)}件を学習")
    elif t == "observation_analysis":
        print(f"   🔬 観測分析: 事実{e.get('facts',0)}件・仮説{e.get('hypotheses',0)}件"
              + (f" [{e.get('fact_summary','')}]" if e.get("fact_summary") else ""))
    elif t == "hallucination_blocked":
        print(f"   🚫 事実照合で却下: {e.get('reason','')} → {e.get('suggestion','')}")
    elif t == "ip_guess_blocked":
        print(f"   🛑 IP推測を却下: {e.get('reason','')} → {e.get('suggestion','')}")
    elif t == "engagement_reset":
        prev = e.get("previous", "")
        print(f"   🧹 新しい標的のため記憶をリセット: {e.get('target','')}"
              + (f"（前回: {prev}）" if prev else "（初回）"))
    elif t == "target_locked":
        print(f"   🎯 ターゲット確定: {e.get('primary','')}"
              + (f"（IP: {e.get('ip','')}）" if e.get("ip") else "")
              + f" 許可: {', '.join(e.get('allowed_hosts',[]))}")
    elif t == "target_unresolved":
        print(f"   ⚠ ターゲット未特定: {e.get('message','')}")
    elif t == "target_mismatch_blocked":
        _strk = e.get("streak", 0)
        _sfx = f"（{_strk}回連続）" if _strk and _strk >= 2 else ""
        print(f"   ⛔ ターゲット外を却下: {e.get('offending','')}{_sfx} — {e.get('reason','')}")
    elif t == "target_expanded":
        _rel = e.get("relation", "")
        _par = e.get("parent", "")
        print(f"   🌿 ターゲット拡張[{e.get('status','')}]: {e.get('target','')} "
              + (f"←[{_rel}]← {_par} " if _par else "")
              + f"(信頼{e.get('confidence','')}) 根拠: {e.get('evidence','')[:50]}")
    elif t == "target_reflection":
        print(f"   🔭 ターゲット振り返り: 拡張{e.get('expanded',0)}件・却下{e.get('rejected',0)}件 "
              f"源: {e.get('sources',{})}")
        _rels = e.get("relations", {})
        if _rels:
            print(f"      関係の有効性: 有効{_rels.get('useful',{})} / "
                  f"無効{_rels.get('ineffective',{})}")
    elif t == "strategy_switch":
        print(f"   ♻ 戦略変更: {e.get('frm','')} → {e.get('to','')}（{e.get('reason','')}）")
    elif t == "dead_end_detected":
        print(f"   🧭 行き止まり検知: 仮説{e.get('regenerated',0)}件を再生成して別経路へ")
    elif t == "exploration_metrics":
        print(f"   📊 探索メトリクス: 深さ{e.get('exploration_depth',0)} "
              f"独自仮説{e.get('unique_hypotheses',0)} 行き止まり{e.get('dead_ends',0)} "
              f"新規経路{e.get('novel_paths',0)} 戦略切替{e.get('strategy_switches',0)}")
    elif t == "retry_strategy":
        print(f"   🔄 作戦変更（{e.get('fail_streak')}回目の失敗）")
    elif t == "replan":
        print(f"   🗺 再計画: {' → '.join(e.get('steps',[]))}")
    elif t == "step_plan":
        print(f"   📋 手順プラン: {' → '.join(e.get('steps',[]))}")
    elif t == "reflect":
        ls = e.get('lessons', [])
        print(f"   🎓 自己評価 {e.get('score',0)}点" + (f" / 教訓: {'; '.join(ls)}" if ls else ""))
    elif t == "loop_detected":
        print(f"   🔁 {e.get('message','LOOP DETECTED')}")
    elif t == "plan_failed":
        print(f"   ⚠ plan生成失敗: {e.get('message','')}")
    elif t == "prompt_warning":
        print(f"   📏 {e.get('message','プロンプトが大きすぎます')}")
    elif t == "decision_quality":
        print(f"   🧭 判断品質: 手{e.get('decisions',0)} "
              f"成功率{e.get('decision_success_rate',0)} "
              f"幻覚率{e.get('hallucination_rate',0)} "
              f"Scope違反{e.get('scope_violations',0)} "
              f"戦略切替{e.get('strategy_switches',0)} "
              f"[trace:{e.get('trace_id','')[:8]}]")
    elif t == "duplicate":
        print(f"   ↻ 重複検知 → 実行せず別の手を促す: {e['action'].get('type')}")
    elif t == "action":
        print(f"   手{e['turn']}: {e['action']}")
    elif t == "exec_result":
        print(f"     結果: {e['result'][:200]}")
    elif t == "memory_save":
        print(f"   記憶: {e['summary']}（{'成功' if e['success'] else '未完了'}）")
    elif t == "judge":
        print(f"   判定: {'達成' if e['done'] else '未達成'} — {e.get('reason','')}")
    elif t == "objective_giveup":
        print(f"   ※ 上限{e['turns']}手でも未達成のため打ち切り: {e['objective']}")
    elif t == "resumed":
        if e.get("appended"):
            print(f"   ↩ 前回のゴールに追加要望を足して続行（目的 {len(e['objectives'])}件に拡張）")
        else:
            print(f"   ↩ 前回の続きから再開: 目的 {e['index']+1}/{len(e['objectives'])}（{e['done_count']}件完了済み）")
    elif t == "installs":
        print("\n--- インストール済み ---")
        for it in e["items"]:
            mk = "✓" if it["success"] else "✗"
            print(f"  {mk} [{it['manager']}] {', '.join(it['packages'])}")
    elif t == "final_report":
        print("\n" + "=" * 48)
        print(f"【最終結果】ゴール: {e['goal']}")
        print(f"  達成 {e['done']}/{e['total']} 目的")
        for i, r in enumerate(e["results"], 1):
            mark = "✓" if r["success"] else "×"
            print(f"  {mark} 目的{i}: {r['objective']}")
            print(f"      → {r['summary']}")
        print("=" * 48)
    elif t == "run_done":
        print(f"\n[STEP3] 完了。長期記憶 計{e['ltm_count']}件")
    elif t == "error":
        print(f"   ERROR: {e['msg']}")


def _emit(**e) -> None:
    try:
        stats.observe("flow", e)   # 行動履歴を記録（得意分野/性格/グラフ用）
    except Exception:
        pass
    # Phase5: Decision Provenance へ全イベントを受動記録（本体は止めない）
    try:
        _p = _PROV_REF.get("prov")
        if _p is not None:
            _p.observe(e)
    except Exception:
        pass
    f = _HOOK["emit"]
    (f or _console)(e)


# ---- 共通: 役割プロンプト＋入力を送り、検証して受け取る ---- #
def ask_role(prompt_role: str, route_role: str, user_text: str, handler,
             task_text: str = ""):
    sp = fast_connect.load_prompt(prompt_role)
    tracker = OutputTracker()          # 同じ崩れた出力の繰り返しを見張る
    last = None
    note = ""                          # 次回に添える噛み砕いた指摘
    # 動的ルーティング: 能力ベクトルから最適モデルを選び、この役割の先頭に一時挿入
    _routed = None
    try:
        if _ROUTING["on"] and task_text:
            import router
            import providers.registry as _reg
            pick = router.route(route_role, task_text)
            if pick:
                prov, mdl = pick
                cur = _reg.ROLE_ROUTES.get(route_role, [])
                if not cur or tuple(cur[0]) != (prov, mdl):
                    _routed = list(cur)        # 退避
                    _reg.ROLE_ROUTES[route_role] = [(prov, mdl)] + \
                        [t for t in cur if tuple(t) != (prov, mdl)]
                    _emit(type="routed", role=route_role,
                          model=f"{prov}/{mdl}", task=task_text[:60])
    except Exception:
        _routed = None
    try:
        return _ask_role_inner(prompt_role, route_role, user_text, handler, sp,
                               tracker, note)
    finally:
        if _routed is not None:
            import providers.registry as _reg
            _reg.ROLE_ROUTES[route_role] = _routed   # 元に戻す（並列汚染防止）


def _ask_role_inner(prompt_role, route_role, user_text, handler, sp,
                    tracker, note):
    last = None
    for attempt in range(RETRIES):
        msgs = [{"role": "system", "content": sp},
                {"role": "user", "content": user_text}]
        if attempt and note:
            msgs.append({"role": "user", "content": note})
        _emit(type="llm_request", role=route_role, prompt_role=prompt_role,
              attempt=attempt + 1,
              messages=[m for m in msgs if m.get("role") != "system"])  # systemは出さない
        # LLM呼び出し自体が失敗（ネットワーク/レート制限/プロバイダ障害）することがある。
        # ここで捕捉し、少し待ってリトライ＋ローカル(ollama)フォールバックする。
        try:
            res = ask(route_role, msgs)
        except Exception as ex:
            last = str(ex)
            _emit(type="llm_error", role=route_role, attempt=attempt + 1,
                  error=str(ex)[:300])
            # ローカルにフォールバックして即復帰を試みる
            fb = _retry_local(route_role, msgs, handler)
            if fb is not None:
                _emit(type="local_fallback", role=route_role, reason="provider_error")
                return fb
            # 最終試行でなければ指数バックオフで待って再試行
            if attempt < RETRIES - 1:
                import time
                time.sleep(min(8.0, 1.5 * (2 ** attempt)))
                continue
            break
        _emit(type="llm_response", role=route_role, model=res.model, content=res.content)

        data, err = handler(res)
        if not err:
            _emit(type="parsed", role=route_role, data=data)
            return data

        # LLMが空応答を返した（プロバイダ不調・レート制限等）。修復不能なので
        # ローカルへフォールバックし、ダメなら次の試行へ（repairには回さない）。
        if not (res.content or "").strip():
            _emit(type="empty_response", role=route_role, attempt=attempt + 1)
            fb = _retry_local(route_role, msgs, handler)
            if fb is not None:
                _emit(type="local_fallback", role=route_role, reason="empty_response")
                return fb
            last = err
            note = "前回は空の応答だった。必ずJSONを1つだけ出力すること。"
            continue

        # クラウドLLMがポリシーで生成を拒否した可能性を検知
        if _looks_like_refusal(res.content):
            _emit(type="provider_refusal", role=route_role,
                  content=res.content[:200],
                  hint=("使用中のLLMがこのリクエストの生成を拒否した可能性があります。"
                        "/experts ページで該当ツールの専門家、または役割のプロバイダを "
                        "ollama（ローカル）に切り替えると、プロバイダのポリシーに依存せず実行できます。"))
            # ローカル(ollama)が使えるなら自動で1回フォールバックを試す
            fb = _retry_local(route_role, msgs, handler)
            if fb is not None:
                _emit(type="local_fallback", role=route_role)
                return fb

        # JSONが崩れた。前回と「まったく同じ崩れ方」かを見て、説明を変える
        repeated = tracker.is_repeat(res.content)
        tracker.remember(res.content)
        last = err
        note = explain_bad_json(err, repeated,
                                jc.contract_hint(prompt_role)
                                or jc.contract_hint(route_role))  # 契約要点も添える
        _emit(type="parse_error", role=route_role, attempt=attempt + 1,
              error=err, repeated=repeated, message=note)

        if not repeated:
            # 違う崩れ方なら、まず安価な修復役に直させてみる
            fixed = repair_json(res.content, err, handler,
                                jc.contract_hint(prompt_role) or jc.contract_hint(route_role))
            if fixed is not None:
                _emit(type="repaired", role=route_role, data=fixed)
                return fixed
        # 同じ崩れ方の繰り返しは修復に回さず、noteを添えて本人に出し直させる
    raise RuntimeError(f"{prompt_role}役の出力が不正: {last}")


# クラウドLLMの典型的な拒否フレーズ（JSONが無く、これらを含むと拒否とみなす）
_REFUSAL_MARKERS = (
    "i cannot", "i can't", "i can not", "i'm unable", "i am unable",
    "i won't", "i will not", "cannot assist", "can't assist", "cannot help",
    "can't help", "unable to assist", "against our", "usage polic", "content polic",
    "not able to provide", "i'm sorry", "i am sorry",
    "お応えできません", "対応できません", "できかねます", "支援できません",
    "お手伝いできません", "ポリシー", "違反",
)


def _looks_like_refusal(text: str) -> bool:
    """LLM応答がポリシー拒否っぽいか（JSONを含まず拒否語を含む）を判定。"""
    if not text:
        return False
    if "{" in text:                      # JSONらしきものがあれば拒否ではない
        return False
    low = text.lower()
    return any(m in low for m in _REFUSAL_MARKERS)


def _retry_local(route_role: str, msgs: list, handler):
    """ローカル(ollama)で1回だけ再試行する。成功すればデータ、無理ならNone。"""
    try:
        import providers.registry as reg
        if "ollama" not in reg.provider_names():
            return None
        models = []
        try:
            models = reg.get_provider("ollama").list_models()
        except Exception:
            pass
        model = models[0] if models else ""
        res = reg.ask_direct("ollama", model, msgs, role=route_role)
        data, err = handler(res)
        return data if not err else None
    except Exception:
        return None


def repair_json(broken: str, err: str, handler, contract: str = ""):
    """壊れた出力をLLMに修復させ、検証に通れば dict を返す。失敗なら None。"""
    # 入力が空（LLMが何も返さなかった）場合は修復不能。呼び出し側の再試行に任せる。
    if not broken or not broken.strip():
        return None
    _emit(type="fn", fn="repair_json", note="壊れたJSONをLLMで修復中")
    msgs = [
        {"role": "system",
         "content": "あなたはJSON修復器です。入力の壊れたJSONを意味を変えずに正しい"
                    "JSONに直し、JSONのみを出力してください。説明文・コードフェンス禁止。"},
        {"role": "user", "content": (f"エラー: {err}\n"
                                     + (f"正しい形: {contract}\n" if contract else "")
                                     + f"壊れた出力:\n{broken[:2000]}")},
    ]
    try:
        res = ask("judge", msgs)
        data, e2 = handler(res)
        return data if not e2 else None
    except Exception:
        return None


def make_goal(request: str) -> dict:
    _emit(type="fn", fn="make_goal", note="goal役: 要望をゴール/目的/タグに分解")
    req = request
    # ペンテスト/偵察モードでは、目的に「実証(エクスプロイト)」フェーズを必ず含めるよう促す。
    # これが無いと偵察・列挙だけで目的完了とみなされ、エクスプロイトまで到達しない。
    if modes.get_mode() in _OFFENSIVE_MODES:
        req = (request
               + "\n\n【ペネトレーションテストの目的分解ルール】"
               "これは許可された隔離環境への診断です。目的(objectives)は recon で"
               "終わらせず、kill-chainに沿って必ず実証まで含めること: "
               "偵察→列挙→脆弱性特定→『脆弱性を実証してアクセス/フラグを取得する』"
               "→（可能なら権限昇格）→レポート。"
               "特に『発見した脆弱性を実際にエクスプロイトして成果（フラグ・認証情報・"
               "セッション）を得る』という目的を必ず1つ以上、明示的に入れること。"
               "「調べる」「確認する」だけの目的で終えてはならない。")
    return ask_role("goal", "goal", req, jc.handle_goal)


def _additional_objectives(request: str, prev_goal: str, tags: list) -> list:
    """完了済みセッションに対する追加要望を、新しい目的リストに分解する。
    既存ゴールの文脈を踏まえて、続きとして実行すべき目的だけを返す。"""
    _emit(type="fn", fn="additional_objectives", note="goal役: 追加要望を目的に分解")
    try:
        ctx = (f"これまでのゴール: {prev_goal}\n"
               f"そのゴールは既に完了している。ユーザーから次の追加要望が来た:\n{request}\n"
               "この追加要望を達成するための目的だけを分解して返すこと。")
        data = ask_role("goal", "goal", ctx, jc.handle_goal)
        objs = data.get("objectives", [])
        return [o for o in objs if isinstance(o, str) and o.strip()]
    except Exception:
        return []


def _is_empty_action(action: dict) -> bool:
    """planが実質的に空の手を出したか判定する。
    typeはあっても中身（command/message/code/path等）が空なら「空プラン」とみなす。
    空プランをそのまま実行すると無意味な結果が返り、ループの原因になる。"""
    if not isinstance(action, dict) or not action.get("type"):
        return True
    t = action.get("type")
    # 各typeの実体フィールドが空（空白のみ含む）なら空とみなす
    key_by_type = {
        "command": "command", "assist": "message", "code": "code",
        "file": "path", "web_search": "query", "tool": "name",
    }
    key = key_by_type.get(t)
    if key is not None:
        val = action.get(key, "")
        if not isinstance(val, str) or not val.strip():
            return True
    return False


def plan_next_action(stm: ShortTermMemory, related: list[str],
                     feedback: str = "") -> dict:
    _emit(type="fn", fn="plan_next_action", note="system役: 次の1手を計画")
    env_line = shell.describe()
    ssh_line = ssh_session.describe()
    if ssh_line:
        env_line = ssh_line + " ／ コマンドはこの接続先で実行される"
    inst = installs.installed_list()
    inst_line = ""
    if inst:
        names = ", ".join(f"{i['manager']}:{i['package']}" for i in inst[:20])
        inst_line = f"\n【導入済み】{names}（これらは再インストール不要。再度installしない）"
    # 知識グラフから関連知識を引いてLLMに渡す（Wikipedia的に関連をたどる）
    know_line = ""
    lesson_line = ""
    sem_line = ""
    try:
        ltm = _LTM_REF.get("ltm")
        if ltm:
            q = stm.current_objective + " " + stm.goal
            facts = ltm.related_knowledge(q)
            if facts:
                know_line = "\n【関連知識（記憶グラフ）】" + " ／ ".join(facts)
            # ペンテスト攻撃状態を注入（状態追跡で次手を賢くする＝コンテキスト喪失対策）
            try:
                import modes as _modes
                if _modes.get_mode() in _OFFENSIVE_MODES:
                    import pentest_kg
                    kg_line = pentest_kg.summary_for_planner(ltm)
                    if kg_line:
                        know_line += kg_line
            except Exception:
                pass
            # 過去の教訓を注入（使うほど賢くなる核心）
            lessons = ltm.relevant_lessons(q)
            if lessons:
                lesson_line = "\n【過去の教訓】" + " ／ ".join(
                    f"{l['lesson']}" for l in lessons)
            # 自動適用ルール（経験学習の最上位＝失敗から作った行動規範）を注入
            try:
                rules = ltm.relevant_rules(q, limit=5)
                if rules:
                    lesson_line += "\n【自動適用ルール（過去の失敗から学習・信頼度順）】" + " ／ ".join(
                        f"{r['condition']}→{r['directive']}(信頼{r.get('confidence',0)})"
                        for r in rules)
                    # 適用したルールIDを記録し、次の結果で成否を帰属させる（Phase2）
                    _APPLIED_RULES["ids"] = [r["id"] for r in rules]
                    for r in rules:
                        ltm.mark_rule_used(r["id"])
            except Exception:
                pass
            # 意味的に近い過去の記憶
            sims = ltm.semantic_search(q, limit=3)
            if sims:
                sem_line = "\n【類似した過去の経験】" + " ／ ".join(
                    f"{s['goal']}: {s['summary'][:40]}" for s in sims)
            # 再利用できる既存スキル（昇格段階を考慮して提示＝賢くなる核心）
            skills = ltm.relevant_skills(q)
            if skills:
                # 提示したスキルIDを記録（目的完了時に成否をmark_skill_used）
                for s in skills:
                    if s.get("id"):
                        _USED_SKILLS["ids"].add(s["id"])
                try:
                    import skill_system
                    sem_line += skill_system.format_for_planner(skills)
                except Exception:
                    sk = []
                    for s in skills:
                        sk.append(f"「{s['name']}」: {' → '.join(s['steps'])}")
                    sem_line += "\n【再利用できるスキル】" + " ／ ".join(sk)
    except Exception:
        pass
    # 使えるツール一覧を提示（type:"tool" で呼べる）
    tools_line = ""
    try:
        import tools.registry as _tr
        tools_line = "\n【使えるツール（type:\"tool\", name, args で呼ぶ）】\n" + _tr.specs_text()
    except Exception:
        pass
    kali_line = ""
    try:
        if ssh_session.is_connected():
            kali_line = kali_tools.prompt_text()
    except Exception:
        pass
    steps_line = ""
    if _STEPS.get("plan"):
        steps_line = "\n【今回の手順プラン】" + " → ".join(_STEPS["plan"]) + \
                     "（この流れに沿って1手ずつ進める）"
    # 攻撃対象インベントリと自動導出した攻撃経路を注入（点でなく繋がりで判断させる）
    engage_line = ""
    try:
        import engagement
        with engagement.Engagement() as _eg:
            txt = _eg.prompt_text()
            # 取得情報の量と質を評価し、現在どの段階にいるべきかを planner に明示
            try:
                import phase_readiness as _phr
                _assess = _phr.assess(_eg.summary(), _eg.attack_paths(),
                                      _phr.quality_metrics(_eg.full_state()))
                if _assess["quantity"] > 0 or _assess["quality"] > 0:
                    txt = (txt or "") + (
                        f"\n【情報の充実度】量={_assess['quantity']} 質={_assess['quality']} "
                        f"／ 推奨段階: {_assess['next_phase']} ／ {_assess['reasons']}")
            except Exception:
                pass
        if txt:
            engage_line = "\n" + txt
    except Exception:
        pass
    # Phase2: 世界状態（観測事実・検証中の仮説・行き止まり）を planner へ注入。
    # これにより「事実に基づく行動」を促し、推測ベースの暴走を抑える。
    world_line = ""
    try:
        _world = _WORLD_REF.get("world")
        if _world is not None:
            wt = _world.prompt_text()
            if wt:
                world_line = wt
    except Exception:
        pass
    # Phase3: 探索エンジンが「次に試すべき仮説」を選び、planner へ誘導注入。
    # confidenceだけでなく novelty/diversity/repetition を加味して局所最適化を防ぐ。
    explore_line = ""
    try:
        eng = _EXPLORE_REF.get("engine")
        _world = _WORLD_REF.get("world")
        strat = _STRATEGY_REF.get("engine")
        if eng is not None and _world is not None:
            hyps = _world.open_hypotheses()
            if hyps:
                # 戦略の優先順で仮説を並べ替えてから探索選択
                if strat is not None:
                    try:
                        hyps = strat.reorder_hypotheses(hyps)
                    except Exception:
                        pass
                chosen = eng.peek_hypothesis(hyps)
                if chosen:
                    # 今ターンの選択を保存（実行結果を同一キーで帰属させるため）
                    _CHOSEN_HYP["hyp"] = chosen
                    sc = chosen.get("_score", {})
                    steps = chosen.get("next_steps") or []
                    explore_line = (
                        f"\n【探索エンジンの推奨仮説】{chosen.get('description','')}"
                        f"（探索スコア={sc.get('total','?')}: 信頼{sc.get('confidence','?')}"
                        f"+新規{sc.get('novelty','?')}+多様{sc.get('diversity','?')}"
                        f"-反復{sc.get('repetition_penalty','?')}）"
                        + (f" 検証手段: {steps[0]}" if steps else "")
                        + "。同じ失敗経路に固執せず、この仮説の検証を優先すること。")
                    if strat is not None:
                        explore_line += f"\n【現在の戦略】{strat.summary()}"
    except Exception:
        pass
    # 対象を明示注入（架空IPの幻覚を防ぐ最重要コンテキスト）
    target_line = ""
    try:
        _ctx = _TARGET.get("ctx")
        _t = _TARGET.get("info")
        if _ctx is not None and _ctx.get("target_locked"):
            primary = _ctx["primary_target"]
            tgt_id = _ctx.get("primary_ip") or primary
            allowed = ", ".join(list(_ctx.get("allowed_hosts", ()))
                                + list(_ctx.get("allowed_networks", ())))
            target_line = (f"\n【攻撃対象（厳守・変更不可）】{primary}"
                           + (f"（IP: {_ctx['primary_ip']}）" if _ctx.get("primary_ip")
                              else "（DNS未解決：ホスト名で指定）")
                           + f"。スキャン・攻撃・アクセスは必ず {tgt_id} に対してのみ行うこと。"
                           + f" 許可対象は [{allowed}] のみ。"
                           "これ以外のホスト（example.com / google.com / testphp.vulnweb.com 等）"
                           "や架空IP(192.168.x.x/10.x.x.x)を対象にしたコマンドは実行ガードで"
                           "却下され実行されない。対象は唯一・固定。推測だけで対象を増やすのは禁止。")
            # Phase4.2: 到達可能ターゲットを Evidence Chain（証拠経路）付きで提示。
            # 「なぜそこを探索できるか」をLLMに説明し、scope外への逸脱を防ぐ。
            try:
                _w = _WORLD_REF.get("world")
                if _w is not None:
                    _reach = [t["target"] for t in _w.trusted_targets()
                              if t.get("source") not in ("user", "DNS Lookup")]
                    _cand = _w.candidate_targets()
                    if _reach:
                        target_line += "\n【証拠経路で到達可能なターゲット（実行可）】"
                        for _rt in _reach:
                            target_line += "\n  ・" + _w.chain_explanation(_rt)
                    if _cand:
                        target_line += ("\n【候補ターゲット（証拠経路なし・未実行）】"
                                        + ", ".join(f"{c['target']}({c['source']})"
                                                    for c in _cand)
                                        + "。Rootからの証拠経路(Evidence Chain)を確立する"
                                        "まで実行対象にできない。DNS/リダイレクト/リンク等で"
                                        "実際に到達経路を観測してから対象にすること。")
            except Exception:
                pass
        elif _t and _t.get("host"):
            tgt_id = _t["ip"] or _t["host"]
            target_line = (f"\n【攻撃対象（厳守）】{_t['host']}"
                           + (f"（IP: {_t['ip']}）" if _t['ip'] else "（DNS未解決）")
                           + f"。スキャンや攻撃は必ず {tgt_id} に対して行うこと。"
                           "192.168.x.x や 10.x.x.x 等の架空・例示IPを絶対に使わないこと。")
    except Exception:
        pass
    context = (f"【実行環境】{env_line}（この環境で動くコマンドを出すこと）{target_line}"
               f"{inst_line}{know_line}{lesson_line}{sem_line}{steps_line}{tools_line}{kali_line}{engage_line}{world_line}{explore_line}\n\n"
              + stm.build_context(related))
    if feedback:                       # 重複時など、前段の指摘を文脈に添えて出し直させる
        context += f"\n\n【注意】{feedback}"
    return ask_role(modes.system_prompt_role(), "plan", context, jc.handle_task,
                    task_text=stm.current_objective + " " + stm.goal)


def execute_action(action: dict) -> str:
    _emit(type="fn", fn="execute_action", note="executor: 行動を実行(承認/dry)")
    approver = _HOOK["approver"]
    if approver is None:
        # 承認関数が無い場合のフォールバック。
        # dry_run時は実際には実行しないので自動承認でよい。
        # 端末input(_ask_approval)はWeb/スレッド環境で固まるため使わない。
        approver = executor.auto_yes if _HOOK["dry"] else executor._ask_approval
    return executor.run_action(
        action,
        approver=approver,
        dry_run=_HOOK["dry"],
    )


def _objective_facts(stm: ShortTermMemory) -> str:
    """judge前の客観チェック。作成したはずのファイルが実在するか等を事実として添える。"""
    import os as _os
    facts = []
    ws = executor.WORKSPACE
    for r in stm.records:
        act = r.action if isinstance(r.action, dict) else {}
        # file書き込み / code保存 で path があれば実在確認
        path = act.get("path")
        if path and act.get("type") in ("file", "code"):
            full = _os.path.join(ws, path) if not _os.path.isabs(path) else path
            exists = _os.path.exists(full)
            facts.append(f"ファイル {path}: {'実在する' if exists else '存在しない'}")
    if not facts:
        return ""
    return "\n\n【客観的事実（システム確認済み）】\n" + "\n".join(facts)


def is_objective_done(stm: ShortTermMemory) -> bool:
    """judge役LLMに、これまでの行動と結果から目的達成を判定させる。
    事前にファイル実在などの客観的事実を確認して judge に渡す（嘘の完了を防ぐ）。"""
    _emit(type="fn", fn="is_objective_done", note="judge役: 目的の達成を判定")
    history = "\n".join(f"{r.action} → 結果: {r.result}" for r in stm.records)
    facts = _objective_facts(stm)
    # エクスプロイト系の目的では「実証できたか」を厳格に問う追記
    strict = ""
    obj = stm.current_objective
    exploit_obj = any(k in obj for k in
                      ("エクスプロイト", "exploit", "実証", "侵入", "アクセス",
                       "フラグ", "認証回避", "権限昇格", "privesc", "取得"))
    if modes.get_mode() in _OFFENSIVE_MODES and exploit_obj:
        strict = ("\n\n【厳格判定】この目的は『脆弱性の実証・侵入・成果の取得』を求めている。"
                  "偵察・列挙・スキャン・「脆弱性がありそう」という観察だけでは done=false。"
                  "実際にエクスプロイトが成功し、成果（フラグ・認証情報・セッション・"
                  "アクセス確認）が結果に現れている場合のみ done=true とすること。")
    try:
        data = ask_role("judge", "judge",
                        f"目的: {stm.current_objective}\n\nこれまでの行動と結果:\n{history}{facts}{strict}",
                        jc.handle_judge)
        _emit(type="judge", done=data["done"], reason=data.get("reason", ""))
        return bool(data["done"])
    except Exception as ex:                       # 判定不能なら未完了扱い（ループ継続）
        _emit(type="judge", done=False, reason=f"判定失敗: {ex}")
        return False


DEBATE_ROUNDS = 2     # planner↔critic の最大往復回数


def plan_with_debate(stm: ShortTermMemory, related: list, feedback: str) -> dict:
    """plannerが1手を提案し、criticが批評、合意するまで往復して最終案を返す。
    多エージェント協調。dry_run時はcriticを省いて速度優先。"""
    try:
        action = plan_next_action(stm, related, feedback)
    except Exception as ex:
        # 全リトライが空応答等で失敗 → クラッシュ・空応答ループを避け、
        # 安全な「中身なし」アクションを返す。呼び出し側の empty_streak が
        # これを検知し、連続すれば打ち切る。
        _emit(type="plan_failed", error=str(ex)[:200],
              message="plan役が手を出せませんでした（空応答/不正出力）")
        return {"type": "assist", "message": "",
                "reason": f"plan生成失敗: {str(ex)[:120]}"}
    if _HOOK.get("dry"):
        return action
    import json as _j
    # criticに渡す「実行履歴」と「失敗済み行動」（誤りや再実行を検出させる）
    history = "\n".join(f"{i}. {r.action} → {r.result}"
                        for i, r in enumerate(stm.records, 1)) or "（まだ無し）"
    failed = [str(r.action) for r in stm.records if is_failure_result(r.result)]
    failed_txt = ("\n失敗・無効だった行動（これらの再実行は却下せよ）:\n"
                  + "\n".join(f"- {a}" for a in failed[-6:])) if failed else ""
    for rnd in range(DEBATE_ROUNDS):
        try:
            critique = ask_role("critic", "judge",
                f"目的: {stm.current_objective}\n"
                f"これまでの実行履歴:\n{history}{failed_txt}\n\n"
                f"plannerの提案: {_j.dumps(action, ensure_ascii=False)}\n"
                "この提案が、失敗済みの手の再実行や、直前の結果を無視していないか確認せよ。",
                jc.handle_critic)
        except Exception:
            break
        if critique.get("ok"):
            if rnd > 0:
                _emit(type="debate", round=rnd + 1, agreed=True,
                      advice=critique.get("advice", ""))
            return action            # criticが承認 → 合意
        advice = critique.get("advice", "別の手を検討")
        _emit(type="debate", round=rnd + 1, agreed=False, advice=advice,
              action=action)
        # plannerがcriticの指摘を踏まえて出し直す（失敗履歴も明示して別の手を強制）
        revise_fb = (f"critic指摘: {advice}\n"
                     f"前回の提案 {_j.dumps(action, ensure_ascii=False)} は却下された。"
                     f"{failed_txt}\n"
                     "必ず上記と異なる、結果を踏まえた別の手をJSONで出すこと。")
        revised = plan_next_action(stm, related, revise_fb)
        if revised == action:
            # 変わらないなら、もう一段強く別案を要求
            revised = plan_next_action(
                stm, related,
                revise_fb + "\n同じ手は絶対に却下される。手段そのものを変えること。")
            if revised == action:
                return action       # それでも変わらなければ打ち切り（ループ側が処理）
        action = revised
    return action                   # 合意できなくても最終案で進む


def plan_steps(stm: ShortTermMemory, ltm) -> list:
    """目的を複数の手順に分解する（多段階プランニング）。失敗時は空。"""
    _emit(type="fn", fn="plan_steps", note="steps役: 目的を手順に分解")
    try:
        lessons = ltm.relevant_lessons(stm.current_objective + " " + stm.goal)
        hint = ("\n参考になる過去の教訓: " + " ／ ".join(l["lesson"] for l in lessons)) if lessons else ""
        data = ask_role("steps", "plan",
                        f"目的: {stm.current_objective}{hint}", jc.handle_steps)
        return data.get("steps", [])[:6]
    except Exception:
        return []


def generate_skill(stm: ShortTermMemory, ltm) -> None:
    """成功した手順を汎用スキルとして蒸留・保存（Skill Generator）。"""
    _emit(type="fn", fn="generate_skill", note="skill役: 成功手順をスキル化")
    history = "\n".join(f"{r.action} → {r.result}" for r in stm.records)
    try:
        data = ask_role("skill", "judge",
                        f"達成した目的: {stm.current_objective}\n\n行動履歴:\n{history}",
                        jc.handle_skill)
        steps = data.get("steps", [])
        if steps and len(steps) >= 2:
            ltm.add_skill(data.get("name", stm.current_objective[:30]),
                          data.get("description", ""), steps,
                          data.get("tags", stm.tags))
            _emit(type="skill_learned", name=data.get("name", ""),
                  steps=steps)
    except Exception as ex:
        _emit(type="skill_learned", name="", steps=[], error=str(ex))


def reflect_and_learn(stm: ShortTermMemory, ltm, done: bool) -> None:
    """目的完了後に自己評価し、次回に活かす教訓を抽出して記憶する。"""
    _emit(type="fn", fn="reflect_and_learn", note="reflect役: 自己評価して教訓を学習")
    history = "\n".join(f"{r.action} → {r.result}" for r in stm.records)
    try:
        data = ask_role("reflect", "judge",
                        f"目的: {stm.current_objective}（達成={done}）\n\n行動履歴:\n{history}",
                        jc.handle_reflect)
        score = data.get("score", 0)
        for lesson in data.get("lessons", [])[:3]:
            if lesson and len(lesson) >= 2:
                ltm.add_lesson(stm.goal, lesson, score)
        _emit(type="reflect", score=score, lessons=data.get("lessons", []))
        # この目的で提示されたスキルに成否を記録（成功率→昇格判定に使う）
        try:
            for sid in list(_USED_SKILLS["ids"]):
                ltm.mark_skill_used(sid, success=bool(done))
        except Exception:
            pass
        _USED_SKILLS["ids"] = set()   # 次の目的へ向けてリセット
        # 成功かつ高評価なら、手順をスキルとして蒸留・保存（Skill Generator）
        if done and score >= 60:
            generate_skill(stm, ltm)
    except Exception as ex:
        _emit(type="reflect", score=0, lessons=[], error=str(ex))
        _USED_SKILLS["ids"] = set()


def summarize_objective(stm: ShortTermMemory) -> str:
    _emit(type="fn", fn="summarize_objective", note="assistant役: 成果を要約")
    history = "\n".join(f"{r.action} → 結果: {r.result}" for r in stm.records)
    data = ask_role("assistant", "summary",
                    f"次は目的「{stm.current_objective}」の行動と結果です。要約してください。\n{history}",
                    jc.handle_assistant)
    return data["conclusion"] if data["type"] == "summary" \
        else f"要約できず（{data['type']}）"


def short_term_snapshot() -> dict:
    """現在の短期記憶（作業中のゴール/目的/直近の行動）のスナップショット。"""
    stm = _STM_REF.get("stm")
    if not stm:
        return {"active": False}
    return {
        "active": True, "goal": stm.goal, "objectives": stm.objectives,
        "current_index": getattr(stm, "index", 0),
        "current_objective": stm.current_objective if stm.objectives else "",
        "steps": _STEPS.get("plan", []),
        "records": [{"action": r.action, "result": str(r.result)[:200]}
                    for r in stm.records],
    }


def run_agent(request: str, emit=None, approver=None, dry_run: bool = False) -> None:
    _HOOK.update(emit=emit, approver=approver, dry=dry_run)
    _STEPS["plan"] = []          # 前回タスクの手順プランをクリア（残留防止）
    _STM_REF["stm"] = None       # 前回タスクの短期記憶参照をクリア
    _reset_interventions()       # 介入キュー/停止フラグを初期化（前回の残り持ち越し防止）
    kali_tools.maybe_refresh()   # SSH接続中なら1日1回 apt list を更新
    try:
        import providers.registry as _reg
        if stats.observe not in _reg._OBSERVERS:
            _reg.add_observer(stats.observe)   # LLM呼び出しの成否/速度を記録
        if budget._on_event not in _reg._OBSERVERS:
            _reg.add_observer(budget._on_event)   # トークン消費を積算
    except Exception:
        pass
    stm, ltm = ShortTermMemory(max_records=5), LongTermMemory()
    _LTM_REF["ltm"] = ltm
    # 攻撃的モードでは Target Context を1度だけ生成して使い回す
    # （build_context は毎回DNS解決するため、複数回呼ぶとCDN等でIPがブレる）。
    _ctx_shared = None
    _tgt_shared = None
    try:
        if modes.get_mode() in _OFFENSIVE_MODES:
            import core
            _ctx_shared = core.target_manager.build_context(request)
            _tgt_shared = core.target_resolver.resolve_target(request)
    except Exception:
        _ctx_shared = None
        _tgt_shared = None
    # Phase2: 仮説駆動の世界状態を用意（攻撃的モードのみ。事実/仮説/探索履歴を永続）
    _WORLD_REF["world"] = None
    try:
        if modes.get_mode() in _OFFENSIVE_MODES:
            import core
            _w0 = core.WorldState()
            _WORLD_REF["world"] = _w0
            # 新しい標的なら、前回エンゲージメントの記憶（世界状態・攻撃インベントリ）を
            # リセットする。これをしないと前回の 157.7.189.66/CVE 等が混入し誤誘導になる。
            try:
                _new_tgt = ((_ctx_shared or {}).get("primary_target") or "").lower().strip()
                _prev_tgt = _w0.get_meta("engagement_target", "")
                if _new_tgt and _new_tgt != _prev_tgt:
                    _w0.clear()                       # 世界状態を初期化
                    _w0.set_meta("engagement_target", _new_tgt)
                    try:
                        import engagement as _eng0
                        with _eng0.Engagement() as _g0:
                            _g0.clear()               # 攻撃インベントリも初期化
                    except Exception:
                        pass
                    _emit(type="engagement_reset", target=_new_tgt,
                          previous=_prev_tgt)
            except Exception:
                pass
    except Exception:
        _WORLD_REF["world"] = None
    # Phase5: Decision Provenance レコーダと Loop Detector を起動（全モードで記録）。
    # 失敗しても本体は止めない。_emit が全イベントをこのレコーダへ受動的に流す。
    _PROV_REF["prov"] = None
    _LOOP_REF["det"] = None
    try:
        import core
        _prov = core.DecisionProvenance(emit=_emit)
        _prov.start_run(request,
                        goal="",
                        mode=modes.get_mode(),
                        primary_target=((_ctx_shared or {}).get("primary_target") or ""))
        _PROV_REF["prov"] = _prov
        _LOOP_REF["det"] = core.LoopDetector()
    except Exception:
        _PROV_REF["prov"] = None
        _LOOP_REF["det"] = None
    # 対象(ホスト/IP/URL)を目標文から抽出・DNS解決し、プランナーへ明示する。
    # これが無いとLLMが架空IP(192.168.1.10等)を幻覚してスキャンし続ける。
    # Phase4: 不変の Target Context（唯一の信頼源）を run 開始時に1度だけ生成しロックする。
    _TARGET["info"] = None
    _TARGET["ctx"] = None
    try:
        if modes.get_mode() in _OFFENSIVE_MODES and _ctx_shared is not None:
            import core
            ctx = _ctx_shared
            tgt = _tgt_shared
            if ctx.get("target_locked"):
                # 不変化して保持（誤った書き換えを防ぐ）
                _TARGET["ctx"] = core.target_manager.freeze(ctx)
                _TARGET["info"] = tgt
                _emit(type="target_locked",
                      primary=ctx["primary_target"], ip=ctx.get("primary_ip", ""),
                      allowed_hosts=list(ctx["allowed_hosts"]),
                      allowed_networks=list(ctx["allowed_networks"]))
                # 対象を世界状態の事実として登録（各ガードの基準にもなる）
                _w = _WORLD_REF.get("world")
                if _w is not None:
                    try:
                        _w.add_fact("target", ctx["primary_target"],
                                    ctx.get("primary_ip", ""),
                                    confidence=1.0, source="goal")
                        if ctx.get("primary_ip"):
                            _w.add_fact("target_ip", ctx["primary_ip"], "",
                                        confidence=1.0, source="dns")
                        # Phase4.2: 主ターゲットを Scope Graph の Root として登録
                        _w.add_target_node(ctx["primary_target"], "trusted",
                                           source="user", evidence="ユーザー指定",
                                           confidence=1.0, relation="root",
                                           is_root=True)
                        if ctx.get("primary_ip"):
                            _w.add_target_node(ctx["primary_ip"], "trusted",
                                               source="DNS Lookup",
                                               parent=ctx["primary_target"],
                                               evidence="DNS解決", confidence=1.0,
                                               relation="dns")
                    except Exception:
                        pass
            else:
                # 対象を特定できない → ロックせず（実行ガードは素通し）、ユーザーに促す
                _emit(type="target_unresolved",
                      message="目標文から対象ホスト/IPを特定できませんでした。"
                              "対象を明示してください。")
    except Exception:
        _TARGET["info"] = None
        _TARGET["ctx"] = None
    # Phase3: 戦略エンジンをrun単位で用意（成功率の高い探索方針を選ぶ）
    _STRATEGY_REF["engine"] = None
    try:
        if modes.get_mode() in _OFFENSIVE_MODES:
            import core
            _STRATEGY_REF["engine"] = core.StrategyEngine(ltm)
    except Exception:
        _STRATEGY_REF["engine"] = None
    final_results = []
    _emit(type="goal_start", request=request)

    prev = session.load() if session.is_continue(request) else None
    if prev and prev.get("objectives"):
        # 前回セッションを復元（完了済みでも続行可能）
        objs = list(prev["objectives"])
        idx = min(prev.get("index", 0), len(objs))
        final_results = [tuple(r) for r in prev.get("results", [])]
        completed = idx >= len(objs)
        if completed:
            # 全完了済み → ユーザーの追加要望を新しい目的として足してから続行
            extra = _additional_objectives(request, prev["goal"], prev["tags"])
            if extra:
                objs += extra        # 残タスクとして追加（idxは据え置き＝追加分から実行）
            else:
                # 追加目的が作れない場合は、要望を1目的として直接追加
                objs.append(request)
        stm.set_goal({"goal": prev["goal"], "objectives": objs,
                      "tags": prev["tags"]})
        stm.index = idx
        _emit(type="resumed", goal=stm.goal, index=stm.index,
              objectives=stm.objectives, done_count=len(final_results),
              appended=(completed))
    else:
        try:
            goal = make_goal(request)
        except Exception:
            _cleanup_run_refs()        # 早期失敗でも接続を漏らさない
            raise
        stm.set_goal(goal)
    _emit(type="goal_done", goal=stm.goal, objectives=stm.objectives, tags=stm.tags)

    while not stm.finished:
      try:
        related = ltm.as_context_lines(stm.tags)
        _emit(type="objective_start", index=stm.index + 1, total=len(stm.objectives),
              objective=stm.current_objective, related=related)

        _STEPS["plan"] = plan_steps(stm, ltm)   # 多段階プラン: 目的を手順に分解
        if _STEPS["plan"]:
            _emit(type="step_plan", steps=_STEPS["plan"],
                  objective=stm.current_objective)
        # Phase3: 探索エンジンを目的単位で用意（局所最適化を防ぎ多様な経路を探る）
        _EXPLORE_REF["engine"] = None
        _CHOSEN_HYP["hyp"] = None
        try:
            _world = _WORLD_REF.get("world")
            if _world is not None:
                import core
                _EXPLORE_REF["engine"] = core.ExplorationEngine(world=_world)
        except Exception:
            _EXPLORE_REF["engine"] = None

        done = False
        dedup = Deduplicator(history_size=5, loop_threshold=3)
        feedback = ""
        turn = 0
        dup_streak = 0          # 重複が連続した回数（別の手が出せていないサイン）
        fail_streak = 0         # 失敗が連続した回数（戦略変更の判断用）
        empty_streak = 0        # 空プランが連続した回数（planが手を出せていないサイン）
        block_streak = 0        # ターゲット照合却下が連続した回数（同じ禁止IPへの突撃ループ検知）
        _last_blocked = ""      # 直前に却下されたホスト（同一IP連打の検知用）
        hall_streak = 0         # 事実照合却下が連続した回数（事実違反の反復検知）
        # 目的が完了するまでループ（暴走防止に上限あり。達したら打ち切る）
        # ペンテストは偵察→列挙→脆弱性特定→エクスプロイトと手数が要るため上限を増やす
        max_turns = MAX_TURNS_PER_OBJECTIVE
        if modes.get_mode() in _OFFENSIVE_MODES:
            max_turns = MAX_TURNS_PENTEST
        while turn < max_turns:
            # 停止要求があれば安全な区切りで終了
            if _should_stop():
                _emit(type="intervention", kind="stop",
                      message="ユーザーの要求で実行を停止しました")
                done = is_objective_done(stm) if turn else False
                break   # この目的の通常終了処理へ。外側ループが停止を検知して抜ける
            # ユーザーからの介入メッセージを取り込み、次のプランへ指示として注入
            intervention = _drain_interventions()
            if intervention:
                feedback = (f"【ユーザーからの指示】{intervention} "
                            "この指示を最優先で次の手に反映すること。"
                            + (f" 直前の状況: {feedback}" if feedback else ""))
                _emit(type="intervention", kind="steer", message=intervention)
            # plannerとcriticが討論して合意した1手を得る（多エージェント協調）
            action = plan_with_debate(stm, related, feedback)
            feedback = ""

            # 空プラン検知: planが実体のない手（空コマンド等）を出した場合。
            # そのまま実行しても無意味な結果が返りループの原因になるため、
            # 別の手を促し、続くようなら打ち切る。
            if _is_empty_action(action):
                empty_streak += 1
                _emit(type="empty_plan", action=action, streak=empty_streak,
                      message="planが空の手を出しました。具体的な手を要求します。")
                if empty_streak >= 3:
                    _emit(type="loop_detected", action=action,
                          message="LOOP DETECTED: 空のプランが続いたため打ち切ります")
                    stm.add_record(action, "空プランの反復のため打ち切り")
                    break
                feedback = ("前回の手は中身が空だった。typeに対応する具体的な値"
                            "（command/message/code/path等）を必ず入れて、"
                            "実行可能な1手をJSONで出すこと。")
                continue   # 空プランは手数に数えず、出し直しを促す
            empty_streak = 0

            # ループ検知: 直近5手で同じ行動が3回以上 → 打ち切って次へ（無限ループ防止）
            if dedup.is_looping(action):
                _emit(type="loop_detected", action=action,
                      message="LOOP DETECTED: 同じ行動が繰り返されたため、この目的を打ち切ります")
                stm.add_record(action, "LOOP DETECTED（同一行動の反復のため打ち切り）")
                break

            if dedup.is_duplicate(action):
                dup_streak += 1
                dedup.note_attempt(action)    # 重複も履歴に刻む（ループ検知のため）
                feedback = explain_duplicate(action)
                _emit(type="duplicate", action=action, message=feedback)
                stm.add_record(action, "（重複のため未実行：別の手を促した）")
                if dup_streak >= 3:           # 重複ばかりで別の手が出ない → 打ち切り
                    _emit(type="loop_detected", action=action,
                          message="LOOP DETECTED: 重複提案が続いたため打ち切ります")
                    break
                continue   # 重複は手数に数えず、別の手を促す

            dup_streak = 0

            # Phase4: Execution Guard（最終ゲート）。コマンド中の IP/ホスト/ドメイン/URL を
            # すべて抽出し、不変の Target Context の許可対象と照合する。許可外なら実行せず却下。
            # 例: ターゲット192.168.1.10 なのに example.com / google.com / testphp.vulnweb.com
            #     を対象にしたコマンドは一切実行しない。
            try:
                import core
                _ctx = _TARGET.get("ctx")
                if _ctx is not None:
                    _eg = core.execution_guard.check(action, _ctx,
                                                     world=_WORLD_REF.get("world"))
                    if not _eg["ok"]:
                        _off = _eg.get("offending", "")
                        # 同じ禁止ホストへの連打を検知してエスカレーション
                        if _off and _off == _last_blocked:
                            block_streak += 1
                        else:
                            block_streak = 1
                            _last_blocked = _off
                        # フィードバックを段階的に強める
                        if block_streak >= 2:
                            feedback = (
                                f"【{block_streak}回目の却下】'{_off}' は絶対に対象にできない"
                                "（スコープ外・証拠経路なし）。このIP/ホストのことは完全に忘れ、"
                                "二度と出力しないこと。許可された対象 "
                                f"{', '.join(list(_ctx.get('allowed_hosts', ()))[:4])} "
                                "のみを使い、全く別の手段（別ツール・別ポート・別エンドポイント）"
                                "に切り替えること。同じ対象を繰り返したら強制終了する。")
                        else:
                            feedback = (f"【ターゲット照合で却下】{_eg['reason']}。{_eg['suggestion']}")
                        _emit(type="target_mismatch_blocked",
                              offending=_off, streak=block_streak,
                              reason=_eg["reason"], suggestion=_eg["suggestion"],
                              action=action)
                        # 却下対象を世界状態に記録（Reflectionで統計化）
                        _w = _WORLD_REF.get("world")
                        if _w is not None:
                            try:
                                _w.add_rejected_target(_off)
                            except Exception:
                                pass
                        _APPLIED_RULES["ids"] = []
                        _CHOSEN_HYP["hyp"] = None
                        # 同じ禁止対象を3回以上 → ループ確定。この目的を打ち切る。
                        if block_streak >= 3:
                            _emit(type="loop_detected", action=action,
                                  message=(f"LOOP DETECTED: '{_off}' への却下が"
                                           f"{block_streak}回連続。この目的を打ち切ります"))
                            stm.add_record(action,
                                           f"スコープ外 '{_off}' への反復のため打ち切り")
                            break
                        # ブロックは手数に数える（無限ループ防止）
                        turn += 1
                        continue   # 実行せず、正しい対象で出し直させる
                else:
                    # Target Context が無い場合でも、IP推測だけは従来ガードで止める
                    _tinfo = _TARGET.get("info")
                    if modes.get_mode() in _OFFENSIVE_MODES or _tinfo:
                        _ip_v = core.hallucination_guard.validate_ip(action, _tinfo)
                        if not _ip_v["ok"]:
                            feedback = (f"【IP推測を却下】{_ip_v['reason']}。{_ip_v['suggestion']}")
                            _emit(type="ip_guess_blocked",
                                  reason=_ip_v["reason"], suggestion=_ip_v["suggestion"],
                                  action=action)
                            _APPLIED_RULES["ids"] = []
                            _CHOSEN_HYP["hyp"] = None
                            continue
            except Exception:
                pass

            # Phase2: ハルシネーション防止。世界状態の事実に反する行動を弾く。
            # 例: 事実=Apacheのみ なのに Nginx の脆弱性調査 → 却下して事実準拠を促す。
            try:
                import core
                _world = _WORLD_REF.get("world")
                if _world is not None and modes.get_mode() in _OFFENSIVE_MODES:
                    _v = core.hallucination_guard.validate(action, _world)
                    if not _v["ok"]:
                        hall_streak += 1
                        feedback = (f"【事実照合で却下】{_v['reason']}。{_v['suggestion']} "
                                    "観測した事実に基づいて行動を出し直すこと。")
                        _emit(type="hallucination_blocked",
                              reason=_v["reason"], suggestion=_v["suggestion"],
                              streak=hall_streak, action=action)
                        # 却下した行動に紐づく仮説/ルールの帰属をクリア
                        # （次の実行へ誤って成否が引き継がれないように）
                        _APPLIED_RULES["ids"] = []
                        _CHOSEN_HYP["hyp"] = None
                        # 事実違反を繰り返す場合はループ確定 → 目的を打ち切る
                        if hall_streak >= 3:
                            _emit(type="loop_detected", action=action,
                                  message=("LOOP DETECTED: 事実違反の手が"
                                           f"{hall_streak}回連続。この目的を打ち切ります"))
                            stm.add_record(action, "事実違反の反復のため打ち切り")
                            break
                        turn += 1   # 事実違反も手数に数える（無限ループ防止）
                        continue   # 実行せず、事実準拠の手を促す
            except Exception:
                pass

            turn += 1
            hall_streak = 0           # 実行できた手が出たのでリセット
            block_streak = 0          # 実行できた手が出たのでブロック連打をリセット
            _last_blocked = ""
            _emit(type="action", turn=turn, action=action)
            # Phase5: Loop Detector で同一コマンド/仮説/戦略/行き止まりの反復を検知。
            # 検知したら実行せず、強い再計画指示を出して別の手へ誘導する。
            try:
                _det = _LOOP_REF.get("det")
                if _det is not None:
                    _cmd_text = " ".join(str(action.get(k, "")) for k in
                                         ("command", "url", "name", "query")).strip()
                    _hit = _det.record_command(_cmd_text)
                    if not _hit[0]:
                        _ch = _CHOSEN_HYP.get("hyp")
                        if _ch:
                            _hit = _det.record_hypothesis(
                                _ch.get("text", "") if isinstance(_ch, dict) else str(_ch))
                    if _hit[0]:
                        _emit(type="loop_detected", action=action,
                              loop_kind=_hit[1],
                              message=f"LOOP DETECTED [{_hit[1]}]: {_hit[2]}")
                        feedback = (
                            f"【ループ検知: {_hit[1]}】{_hit[2]}。"
                            "同じ手・同じ仮説の繰り返しは無意味。全く別のツール・"
                            "別のエンドポイント・別の仮説に必ず切り替えること。")
                        _APPLIED_RULES["ids"] = []
                        _CHOSEN_HYP["hyp"] = None
                        # 反復は手数に数える（無限ループ防止）
                        continue
            except Exception:
                pass
            dedup.remember(action)
            try:
                result = execute_action(action)
            except Exception as ex:        # 実行時エラーで止めず、原因をLLMへ渡して次の手で回復させる
                result = f"エラー: {ex}"
                feedback = (f"前の手でエラーが出たよ（{ex}）。"
                            "原因を踏まえて、別のやり方で次の手をJSONで出してね。")
                _emit(type="action_error", error=str(ex), action=action)
            _emit(type="exec_result", result=result)
            stm.add_record(action, result)
            # Phase4: 実行されたターゲットを世界状態に記録（整合性の統計用）
            try:
                _ctx = _TARGET.get("ctx")
                _w = _WORLD_REF.get("world")
                if _ctx is not None and _w is not None:
                    import core
                    for _h in core.execution_guard.extract_hosts(
                            " ".join(str(action.get(k, "")) for k in
                                     ("command", "url", "name", "query"))
                            if isinstance(action, dict) else ""):
                        if _h != "127.0.0.1":
                            _w.add_executed_target(_h)
            except Exception:
                pass
            # Phase2: 観測から事実を抽出→世界状態へ保存し、仮説を生成（仮説駆動）
            try:
                import core
                _world = _WORLD_REF.get("world")
                if _world is not None:
                    _researcher = core.Researcher(ltm)
                    analysis = _researcher.analyze_observation(result, world=_world)
                    # 試した行動を記録（重複探索の防止）
                    _path = ""
                    if isinstance(action, dict):
                        _path = (action.get("command") or action.get("url")
                                 or action.get("name") or "")[:160]
                    if _path:
                        _world.mark_tested(_path,
                                           "fail" if is_failure_result(result) else "ok")
                    if analysis["facts"] or analysis["hypotheses"]:
                        _emit(type="observation_analysis",
                              facts=len(analysis["facts"]),
                              hypotheses=len(analysis["hypotheses"]),
                              fact_summary=core.fact_layer.facts_summary(
                                  [core.Fact(**{k: v for k, v in f.items()
                                                if k in ("type", "name", "value",
                                                         "confidence", "source")})
                                   for f in analysis["facts"]]))
                    # Phase4.1: 観測（実ツール出力）から証拠付きで新ターゲットを抽出。
                    # 証拠由来なので信頼ターゲットへ昇格できる（LLM推測ではない）。
                    try:
                        _parent = ""
                        _ctx = _TARGET.get("ctx")
                        if _ctx is not None:
                            _parent = _ctx.get("primary_target", "")
                        evid = core.evidence_engine.extract_evidence(result, parent=_parent)
                        for ev in evid:
                            # 既に許可済み/信頼済みなら何もしない
                            if _ctx is not None and core.target_manager.host_allowed(
                                    ev["target"], _ctx):
                                continue
                            status = ("trusted"
                                      if ev["confidence"] >= core.evidence_engine.TRUST_THRESHOLD
                                      else "candidate")
                            # Phase4.2: 親は「この観測を生んだホスト」にして
                            # Evidence Chain を正しく伸ばす。観測元が不明なら primary。
                            _rel = core.evidence_engine.relation_for(ev["source"])
                            _parent_host = ev.get("parent") or ""
                            try:
                                _src_hosts = core.execution_guard.extract_hosts(
                                    " ".join(str(action.get(k, "")) for k in
                                             ("command", "url", "name", "query"))
                                    if isinstance(action, dict) else "")
                                # 観測元コマンドの対象で、到達可能なものを親にする
                                for _sh in _src_hosts:
                                    if _world.is_reachable(_sh):
                                        _parent_host = _sh
                                        break
                            except Exception:
                                pass
                            _world.add_target_node(
                                ev["target"], status, source=ev["source"],
                                parent=_parent_host, evidence=ev["evidence"],
                                confidence=ev["confidence"], relation=_rel)
                            _emit(type="target_expanded", target=ev["target"],
                                  status=status, source=ev["source"],
                                  confidence=ev["confidence"], evidence=ev["evidence"],
                                  parent=_parent_host, relation=_rel)
                    except Exception:
                        pass
            except Exception:
                pass
            # Critic で結果を批評し、経験/教訓/ルールとして記憶（経験学習）
            try:
                import core
                _critic = core.Critic(sys.modules[__name__])
                _mm = core.MemoryManager(ltm, sys.modules[__name__])
                _crit = _critic.critique(stm.current_objective, action,
                                         result, goal=stm.goal)
                _mm.record_experience(_critic.to_experience(
                    stm.current_objective, action, result, _crit["success"]))
                _mm.record_critique(stm.current_objective, stm.goal, _crit)
                # 適用したルールの成否を記録（信頼度・成功率を更新）
                try:
                    for _rid in _APPLIED_RULES.get("ids", []):
                        ltm.record_rule_outcome(_rid, _crit["success"])
                    _APPLIED_RULES["ids"] = []
                except Exception:
                    pass
                # 失敗した行動は世界状態の行き止まりに記録（再挑戦しない）
                _world = _WORLD_REF.get("world")
                if _world is not None and not _crit["success"] and isinstance(action, dict):
                    _dp = (action.get("command") or action.get("url")
                           or action.get("name") or "")[:160]
                    if _dp:
                        _world.mark_dead_end(_dp, _crit.get("cause", "")[:80])
                _emit(type="critique", success=_crit["success"],
                      cause=_crit.get("cause", "")[:120],
                      improvement=_crit.get("improvement", "")[:120])
            except Exception:
                pass
            # Phase3: 探索結果を記録し、行き止まり/予算超過なら戦略を切り替える
            try:
                eng = _EXPLORE_REF.get("engine")
                strat = _STRATEGY_REF.get("engine")
                if eng is not None:
                    import core
                    _succ = not is_failure_result(result)
                    # 計画時に選んだ仮説（同一キー）に結果を帰属させる。
                    _hyp = _CHOSEN_HYP.get("hyp")
                    if not _hyp:
                        _ap = ""
                        if isinstance(action, dict):
                            _ap = (action.get("command") or action.get("url")
                                   or action.get("name") or "")
                        _hyp = {"next_steps": [_ap],
                                "description": (action.get("reason", "")
                                                if isinstance(action, dict) else "")}
                    # この実行を1探索として確定（depth/多様性を1回だけ加算）
                    try:
                        eng.select_hypothesis([_hyp], commit=True)
                    except Exception:
                        pass
                    eng.record_result(_hyp, _succ)
                    _CHOSEN_HYP["hyp"] = None
                    _cat = (_hyp.get("category")
                            or core.infer_category(_hyp.get("description", "")))
                    # 探索履歴を永続化
                    try:
                        ltm.record_exploration(stm.current_objective,
                                               _hyp.get("description", "")[:120],
                                               _cat, 0.0, _succ)
                    except Exception:
                        pass
                    # 予算超過 → 戦略を切り替えて探索方向を変える
                    if eng.budget_exceeded() and strat is not None:
                        old = strat.current().get("name", "")
                        new = strat.switch_strategy(avoid=old)
                        eng.note_strategy_switch()
                        if new.get("name") != old:
                            _emit(type="strategy_switch", frm=old,
                                  to=new.get("name", ""),
                                  reason="探索予算超過：方針転換")
                    # 行き止まり/手詰まり → 仮説を再生成して新たな経路を開く
                    if eng.stuck(new_facts=0):
                        try:
                            _world = _WORLD_REF.get("world")
                            _researcher = core.Researcher(ltm)
                            _analysis = _researcher.analyze_observation(result, world=_world)
                            _emit(type="dead_end_detected",
                                  regenerated=len(_analysis.get("hypotheses", [])),
                                  message="手詰まりを検知：仮説を再生成して別経路を探索")
                            feedback = ("現在の経路は行き止まり。新たに生成した仮説に基づき、"
                                        "これまでと異なるカテゴリ（設定/認証/アップロード/API等）"
                                        "を探索すること。")
                        except Exception:
                            pass
            except Exception:
                pass
            # 成功時、攻撃チェーンを知識グラフに記録（状態追跡＝次手の精度向上）
            if not is_failure_result(result):
                try:
                    import modes as _modes
                    if _modes.get_mode() in _OFFENSIVE_MODES:
                        import pentest_kg, re as _re
                        ltm_k = _LTM_REF.get("ltm")
                        if ltm_k and isinstance(action, dict):
                            # URLを文字列の途中からでも正しく抽出（host:port部分のみ）
                            blob = str(action.get("url") or "") + " " + \
                                str(action.get("command") or "")
                            murl = _re.search(r"https?://([^/\s\"']+)", blob)
                            host = murl.group(1) if murl else ""
                            # 成果（フラグ/認証情報）を結果から抽出
                            loot = ""
                            mflag = _re.search(r"FLAG\{[^}]+\}", str(result))
                            if mflag:
                                loot = mflag.group(0)
                            else:
                                # 既知のフラグ的トークン（英数_を含む値）を拾う。
                                # 「password: xxx」のラベルでなく値側を取る。
                                mc = _re.search(
                                    r"(?:pw|pass\w*|cred\w*|token)[\s:=]+([^\s\"']{4,})",
                                    str(result), _re.I)
                                if mc:
                                    loot = mc.group(1)
                            if host and loot:
                                pentest_kg.record_chain(
                                    ltm_k, host, "http",
                                    stm.current_objective[:30],
                                    str(action.get("type", "action")), loot)
                except Exception:
                    pass
            # フェーズ移行判断: 偵察ツールを使った直後、攻撃グラフに溜まった
            # 「情報量と質」を評価し、十分なら次段階（武器化/攻撃）へ進むよう促す。
            # 固定回数でなく、取得情報の充実度で移行を決める。
            if not feedback and modes.get_mode() in _OFFENSIVE_MODES:
                cmdtxt = ""
                if isinstance(action, dict):
                    cmdtxt = (str(action.get("command", "")) + " "
                              + str(action.get("name", ""))).lower()
                _RECON_TOOLS = ("nmap", "nikto", "masscan", "rustscan",
                                "whatweb", "dnsenum", "dnsrecon", "fierce",
                                "gobuster", "ffuf", "enum4linux", "web_scan")
                if any(t in cmdtxt for t in _RECON_TOOLS):
                    try:
                        import engagement as _eng
                        import phase_readiness as _phr
                        with _eng.Engagement() as _g:
                            _summary = _g.summary()
                            _paths = _g.attack_paths()
                            _qual = _phr.quality_metrics(_g.full_state())
                        assess = _phr.assess(_summary, _paths, _qual)
                        _emit(type="phase_readiness",
                              readiness=assess["readiness"],
                              quantity=assess["quantity"],
                              quality=assess["quality"],
                              next_phase=assess["next_phase"],
                              reasons=assess["reasons"])
                        # 偵察が十分（量・質が足りている）なら次段階へ誘導
                        if assess["recon_enough"]:
                            feedback = _phr.feedback_for(assess)
                            _emit(type="phase_nudge",
                                  next_phase=assess["next_phase"],
                                  message=f"情報の量と質から次段階へ移行: {assess['next_phase']}")
                    except Exception:
                        pass
            # 結果がエラー文字列なら「戦略変更」を促す（同じ手の繰り返しを防ぐ）
            if not feedback and is_failure_result(result):
                fail_streak += 1
                # 構造化された再計画指令を生成（型付きの方針転換）
                attempted = ""
                if isinstance(action, dict):
                    attempted = (action.get("command") or action.get("url")
                                 or action.get("name") or "")
                try:
                    import replan as _rp
                    directive = _rp.analyze(result, stm.current_objective, attempted)
                    feedback = (f"前の手は失敗した（{result[:80]}）。{directive['replan_hint']}"
                                " 同じ手は繰り返さないこと。")
                    # 失敗教訓をその場でLTMへ（次の同種objに即反映＝セッション内学習）
                    ltm = _LTM_REF.get("ltm")
                    if ltm:
                        _rp.record_immediate_lesson(ltm, stm.current_objective,
                                                    attempted, result, stm.goal)
                except Exception:
                    hint = "別のコマンド/手段に切り替えて" if fail_streak >= 2 else "原因を直して"
                    feedback = (f"前の手は失敗した（{result[:80]}）。{hint}次の手を出して。"
                                "同じ手は繰り返さないこと。")
                _emit(type="retry_strategy", fail_streak=fail_streak, result=result[:120])
                if fail_streak >= 3:       # 失敗が続く → 戦略を立て直す（動的再計画）
                    # ペンテスト系モードでは攻撃グラフから別の有望な枝を探す（ReAct）
                    branched = False
                    try:
                        if modes.get_mode() in _OFFENSIVE_MODES:
                            import strategist
                            best = strategist.pick_best(stm.current_objective, n=3)
                            if best:
                                _STEPS["plan"] = [best.get("first_action", "")]
                                _emit(type="replan",
                                      steps=[f"[{best['phase']}] {best['hypothesis']}"],
                                      objective=stm.current_objective)
                                branched = True
                    except Exception:
                        pass
                    if not branched:
                        new_steps = plan_steps(stm, ltm)
                        if new_steps:
                            _STEPS["plan"] = new_steps
                            _emit(type="replan", steps=new_steps,
                                  objective=stm.current_objective)
                    fail_streak = 0
            else:
                fail_streak = 0
            if feedback:                   # エラー回復のフィードバックがあれば判定せず次ターンへ
                continue
            if is_objective_done(stm):     # judge役が「達成」と言うまで続ける
                done = True
                break
        if not done:
            _emit(type="objective_giveup", objective=stm.current_objective,
                  turns=turn)

        summary = summarize_objective(stm)
        ltm.save(goal=stm.goal, objective=stm.current_objective,
                 summary=summary, tags=stm.tags, success=done)
        _emit(type="memory_save", summary=summary, success=done,
              objective=stm.current_objective)
        reflect_and_learn(stm, ltm, done)   # 自己評価して教訓を記憶（使うほど賢くなる）
        # Phase3: この目的の探索メトリクスを永続化（Reflectionで活用）
        try:
            eng = _EXPLORE_REF.get("engine")
            if eng is not None:
                ltm.save_exploration_metrics(stm.current_objective, eng.metrics)
                _emit(type="exploration_metrics", **eng.metrics)
            # 戦略の成否は目的単位で1回だけ記録（行動単位だとノイズで成功率が荒れる）
            strat = _STRATEGY_REF.get("engine")
            if strat is not None:
                strat.record_outcome(bool(done))
        except Exception:
            pass
        final_results.append((stm.current_objective, summary, done))
        stm.advance()
        session.save(stm.goal, stm.objectives, stm.index, stm.tags,
                     [list(r) for r in final_results])   # 続行用に進捗を保存
        if _should_stop():       # 停止要求があれば残りの目的へ進まず run を終える
            stm.finish()
            break
      except Exception as ex:        # 目的単位の想定外エラーでも全体は止めない
        _emit(type="objective_error", objective=stm.current_objective, error=str(ex))
        final_results.append((stm.current_objective, f"エラーで中断: {ex}", False))
        _USED_SKILLS["ids"] = set()   # スキルIDが次の目的へリークしないよう確実にリセット
        stm.advance()

    # ===== 最終結果のまとめ表示 =====
    ok = sum(1 for _, _, d in final_results if d)
    _emit(type="final_report", goal=stm.goal, total=len(final_results),
          done=ok, results=[
              {"objective": o, "summary": su, "success": d}
              for (o, su, d) in final_results
          ])
    # Reflectionループ：最近の失敗/成功を振り返り、繰り返す失敗をルール化し、
    # 記憶を統合する（経験→教訓→ルールの昇華＝長時間稼働での自己改善）
    try:
        import core
        _mm = core.MemoryManager(ltm, sys.modules[__name__])
        refl = _mm.reflection_loop()
        cons = refl.get("consolidated", {}) or {}
        if refl.get("new_rules"):
            _emit(type="reflection", failures=refl.get("failures", 0),
                  successes=refl.get("successes", 0),
                  new_rules=refl.get("new_rules", 0))
        # Phase5: Decision品質メトリクス（幻覚率/ループ/Scope違反/Prompt推移）を集計し報告
        try:
            _p = _PROV_REF.get("prov")
            if _p is not None and getattr(_p, "trace_id", ""):
                try:
                    _p._flush_decision()   # 最後の手を確定してから集計
                except Exception:
                    pass
                from core import decision_replay as _dr
                _dq = _dr.run_metrics(_p.trace_id, db_path=_p.db_path)
                if _dq:
                    _emit(type="decision_quality",
                          decisions=_dq.get("decisions", 0),
                          decision_success_rate=_dq.get("decision_success_rate", 0),
                          hallucination_rate=_dq.get("hallucination_rate", 0),
                          scope_violations=_dq.get("scope_violations", 0),
                          strategy_switches=_dq.get("strategy_switches", 0),
                          trace_id=_p.trace_id)
        except Exception:
            pass
        # Phase4.1: ターゲット拡張/却下の振り返り（証拠源の評価・誤検出の学習）
        try:
            _w = _WORLD_REF.get("world")
            if _w is not None:
                tref = _mm.target_reflection(_w)
                if tref.get("expanded") or tref.get("rejected"):
                    _emit(type="target_reflection",
                          expanded=tref.get("expanded", 0),
                          rejected=tref.get("rejected", 0),
                          sources=tref.get("sources", {}),
                          relations=tref.get("relations", {}),
                          chains=tref.get("chains", []))
        except Exception:
            pass
        if any(cons.get(k) for k in ("removed_duplicates", "removed_lessons",
                                     "promoted", "pruned_lessons", "pruned_skills")):
            _emit(type="consolidated", **{k: v for k, v in cons.items()
                                          if k != "rubrics"})
    except Exception:
        # フォールバック: 従来の統合のみ
        try:
            import consolidation
            cons = consolidation.run_full(ltm)
            if any(cons.get(k) for k in ("removed_duplicates", "removed_lessons",
                                         "promoted", "pruned_lessons", "pruned_skills")):
                _emit(type="consolidated", **{k: v for k, v in cons.items()
                                              if k != "rubrics"})
        except Exception:
            pass
    inst = installs.load()
    if inst:
        _emit(type="installs", items=inst)
    # 実runのテレメトリを能力ベクトルへ観測（弱め重み。AgentBench採点を流用）
    try:
        import capabilities
        import agentbench
        import providers.registry as _reg
        # plan役の現モデルを「このrunを主導したモデル」とみなす
        route = _reg.ROLE_ROUTES.get("plan", [])
        if route:
            prov, mdl = route[0][0], route[0][1]
            key = f"{prov}/{mdl}"
            # run内の行動軌跡を簡易ステップ化してルーブリック採点
            steps = []
            for o, su, d in final_results:
                steps.append({"ev": "exec_result", "result": su,
                              "conclusion": ("達成" if d else "未達")})
            ok_rate = (sum(1 for _, _, d in final_results if d)
                       / len(final_results)) if final_results else 0
            rubric = agentbench.score_trajectory(steps, ok_rate >= 0.5)
            capabilities.observe_from_run(key, {
                "planning": rubric["planning"],
                "reflection": rubric["reflection"],
                "tool_usage": rubric["tool_usage"],
                "security": rubric["security"],
            })
    except Exception:
        pass
    # Phase2/3: 世界状態・探索/戦略エンジンの後始末（接続リーク防止）
    _cleanup_run_refs()
    _emit(type="run_done", ltm_count=ltm.count())
    _LTM_REF["ltm"] = None
    ltm.close()


if __name__ == "__main__":
    if "--installs" in sys.argv:        # インストール履歴だけ表示して終了
        print(installs.summary())
        sys.exit(0)
    req = " ".join(sys.argv[1:]) or "プロジェクト直下のPythonファイル一覧を調べてメモにまとめたい"
    run_agent(req)
