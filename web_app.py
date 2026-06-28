# -*- coding: utf-8 -*-
#web_app.py — ブラウザからエージェントを起動するプラグイン的サーバ（標準ライブラリのみ）
"""
起動:  python web_app.py   →  http://127.0.0.1:8770

機能:
  - 役職(role)をカードで表示。クリックで provider/model を選び差し替え（registry.set_primary）
  - get_models() でプロバイダの実モデルを取得して選択肢に出す
  - 要望を投げると run_agent をスレッド実行し、LLMの行動を SSE で超詳細にライブ表示
  - 「実況ナレーター」プラグインの hype 行も同時に流す
依存ゼロ（http.server）。承認はWeb実行のため auto_yes（または dry-run）固定。
"""
import sys as _sys
try:  # WindowsのコンソールをUTF-8に揃える（文字化け防止）
    _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    _sys.stdin.reconfigure(encoding="utf-8")
except Exception:
    pass

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import providers.registry as reg
from plugins.narrator import narrate

HOST, PORT = "127.0.0.1", 8770
_QUEUES: dict[str, queue.Queue] = {}    # run_id -> イベントキュー
_ANSWERS: dict[str, queue.Queue] = {}   # run_id -> 承認回答キュー（ブラウザのYes/No）
_APPROVE_SEQ: dict[str, int] = {}       # run_id -> 承認リクエスト連番
_SUBS: dict[str, list] = {}             # run_id -> ミラー購読キュー群（mapページ等）
_LATEST = {"run_id": None}              # 直近に開始した run_id
# エージェントはモジュール全域のグローバル状態(_WORLD_REF等)を使うため、
# メインrunの同時実行は状態が混線する。1本に制限する（実行中は409を返す）。
_RUN_ACTIVE = {"on": False}


def _publish(run_id: str, item):
    """主キュー＋全ミラー購読者へイベントを配る（mapページも光らせるため）。
    キューが既に破棄済み（stream終了後など）でも例外を出さない。"""
    q = _QUEUES.get(run_id)
    if q is not None:
        q.put(item)
    for sq in list(_SUBS.get(run_id, [])):
        try:
            sq.put(item)
        except Exception:
            pass


def _interactive_approver(run_id: str):
    """
    executor から呼ばれる承認関数。SSEで approval_request を送り、
    ブラウザの /api/approve 応答（_ANSWERS）が来るまで待つ＝対話的承認。
    """
    def approve(prompt: str) -> bool:
        seq = _APPROVE_SEQ.get(run_id, 0) + 1
        _APPROVE_SEQ[run_id] = seq
        _publish(run_id, {"event": "approval_request",
                          "payload": {"id": seq, "prompt": prompt},
                          "hype": "🤔 実行していい？ブラウザで答えてね"})
        ans_q = _ANSWERS.setdefault(run_id, queue.Queue())
        try:
            ans = ans_q.get(timeout=300)     # 最大5分待つ
        except queue.Empty:
            return False                     # 無回答はNo（安全側）
        return bool(ans)
    return approve


def _observer_for(run_id: str):
    def obs(event: str, payload: dict):
        _publish(run_id, {"event": event, "payload": payload,
                          "hype": narrate(event, payload)})
    return obs


def _worker(run_id: str, request: str, dry: bool, interactive: bool = False):
    import agent_loop, executor
    q = _QUEUES[run_id]
    obs = _observer_for(run_id)
    reg.add_observer(obs)
    # 逐次ログを専用LLMで噛み砕いて説明する（膨大ログの可読化）
    try:
        import log_explainer
        explainer = log_explainer.LogExplainer(
            publish=lambda text: _publish(run_id,
                                          {"event": "explain", "payload": {"text": text}}))
    except Exception:
        explainer = None
    # 実況ナレーター（実在LLM）。テンポの良い短い実況を別ペインへ。
    try:
        import llm_narrator
        narrator = llm_narrator.LLMNarrator(
            publish=lambda text: _publish(run_id,
                                          {"event": "narrate", "payload": {"text": text}}))
    except Exception:
        narrator = None

    def flow_emit(e):
        # テンプレ実況(hype)は即時の繋ぎ。実在LLM実況は narrate イベントで遅れて届く。
        _publish(run_id, {"event": "flow", "payload": e, "hype": narrate("flow", e)})
        if explainer is not None:
            try:
                explainer.feed(e)
            except Exception:
                pass
        if narrator is not None:
            try:
                narrator.feed(e)
            except Exception:
                pass
    # 承認方法: dry=承認不要 / interactive=ブラウザで対話承認 / それ以外=自動Yes
    if dry:
        approver = None
    elif interactive:
        approver = _interactive_approver(run_id)
    else:
        approver = executor.auto_yes
    try:
        agent_loop.run_agent(request, emit=flow_emit, approver=approver, dry_run=dry)
        if explainer is not None:
            try: explainer.flush()    # 残りのログも説明して締める
            except Exception: pass
        if narrator is not None:
            try: narrator.flush()
            except Exception: pass
        q.put({"event": "run_done", "payload": {}})
    except Exception as e:
        q.put({"event": "run_error", "payload": {"error": str(e)}})
    finally:
        _RUN_ACTIVE["on"] = False   # 実行終了。次のrunを許可
        try: reg._OBSERVERS.remove(obs)
        except ValueError: pass
        _ANSWERS.pop(run_id, None)
        _APPROVE_SEQ.pop(run_id, None)
        for sq in list(_SUBS.get(run_id, [])):
            sq.put(None)
        _SUBS.pop(run_id, None)
        if _LATEST["run_id"] == run_id:
            _LATEST["run_id"] = None
        q.put(None)  # 終了マーカー


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        # JSONはUTF-8で送るのでcharsetを明示（ブラウザでの文字化け防止）
        if ctype == "application/json":
            ctype = "application/json; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            with open("web/index.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/roles":
            return self._send(200, {"routes": reg.current_routes(),
                                    "providers": reg.provider_names()})
        # Phase5: Decision Provenance / Replay API
        if u.path == "/api/replay/runs":
            try:
                from core import decision_replay as _dr
                return self._send(200, {"runs": _dr.list_runs()})
            except Exception as ex:
                return self._send(200, {"runs": [], "error": str(ex)})
        if u.path == "/api/replay/run":
            try:
                from core import decision_replay as _dr
                tid = parse_qs(u.query).get("trace_id", [""])[0]
                return self._send(200, _dr.get_run(tid))
            except Exception as ex:
                return self._send(200, {"error": str(ex)})
        if u.path == "/api/replay/decision":
            try:
                from core import decision_replay as _dr
                did = parse_qs(u.query).get("decision_id", [""])[0]
                return self._send(200, _dr.get_decision(did))
            except Exception as ex:
                return self._send(200, {"error": str(ex)})
        if u.path == "/api/replay/chain":
            try:
                from core import decision_replay as _dr
                did = parse_qs(u.query).get("decision_id", [""])[0]
                return self._send(200, _dr.provenance_chain(did))
            except Exception as ex:
                return self._send(200, {"error": str(ex)})
        if u.path == "/api/replay/graph":
            try:
                from core import decision_replay as _dr
                tid = parse_qs(u.query).get("trace_id", [""])[0]
                return self._send(200, _dr.decision_graph(tid))
            except Exception as ex:
                return self._send(200, {"error": str(ex)})
        if u.path == "/api/replay/tool_metrics":
            try:
                from core import decision_replay as _dr
                return self._send(200, {"tools": _dr.tool_metrics()})
            except Exception as ex:
                return self._send(200, {"tools": [], "error": str(ex)})
        if u.path == "/api/replay/run_metrics":
            try:
                from core import decision_replay as _dr
                tid = parse_qs(u.query).get("trace_id", [""])[0]
                return self._send(200, _dr.run_metrics(tid))
            except Exception as ex:
                return self._send(200, {"error": str(ex)})
        if u.path == "/api/ollama_status":
            # ollamaの接続状況とモデル一覧を返す（UIで確認できるように）
            try:
                import ollamas.ollama_server as _os
                import ollamas.ollama_control as _oc
                running = _os.is_ollama_running()
                models = []
                if running:
                    try:
                        models = _oc.get_models()
                    except Exception:
                        models = []
                return self._send(200, {
                    "running": running,
                    "base_url": _oc._base_url(),
                    "models": models,
                    "model_count": len(models),
                })
            except Exception as ex:
                return self._send(200, {"running": False, "error": str(ex)})
        if u.path == "/memory":
            with open("web/memory.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/map":
            with open("web/map.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/galaxy":
            with open("web/galaxy.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/tools":
            with open("web/tools.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/attack":
            with open("web/attack.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/replay":
            with open("web/replay.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/experts":
            with open("web/experts.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/benchmark":
            with open("web/benchmark.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/ctf_challenges":
            import ctf_bench as cb
            return self._send(200, {"challenges": cb.load_challenges()})
        if u.path == "/api/ctf_presets":
            import ctf_bench as cb
            return self._send(200, {"presets": cb.practice_presets()})
        if u.path == "/api/test_sets":
            import model_bench as mb
            mb.seed_test_dir()       # 空なら固定問題で初期化
            return self._send(200, {"sets": mb.list_test_sets()})
        if u.path == "/api/bench_results":
            import model_bench as mb
            return self._send(200, mb.load_results())
        if u.path == "/api/capabilities":
            import capabilities as cap
            return self._send(200, {"models": cap.all_vectors(),
                                    "traits": cap.TRAITS})
        if u.path == "/api/consolidate":
            import memory.long_term as _lt
            import consolidation
            try:
                with _lt.LongTermMemory() as ltm:
                    res = consolidation.run_full(ltm)
                return self._send(200, {"ok": True,
                                        "result": {k: v for k, v in res.items()
                                                   if k != "rubrics"}})
            except Exception as ex:
                return self._send(200, {"ok": False, "error": str(ex)})
        if u.path == "/scores":
            with open("web/scores.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/capabilities":
            with open("web/capabilities.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/memory_graph":
            from memory import LongTermMemory
            mem = LongTermMemory()
            try:
                return self._send(200, mem.graph())
            finally:
                mem.close()
        if u.path == "/api/memory":
            from memory import LongTermMemory
            mem = LongTermMemory()
            try:
                return self._send(200, {"memories": mem.all(), "count": mem.count()})
            finally:
                mem.close()
        if u.path == "/api/api_keys":
            import api_keys
            return self._send(200, {"providers": api_keys.all_config()})
        if u.path == "/api/mistral_config":
            from mistral_llm import mistral_control
            import os as _os
            # .env から MISTRAL_API_KEY を含む環境変数名を探す（複数キー対応）
            keys = sorted(k for k in _os.environ if "MISTRAL" in k.upper() and "KEY" in k.upper())
            return self._send(200, {"config": mistral_control.get_config(),
                                    "available_keys": keys})
        if u.path == "/api/mcp_servers":
            from tools import mcp_client
            return self._send(200, {"servers": mcp_client.list_servers()})
        if u.path == "/api/kali_tools":
            import kali_tools
            return self._send(200, {"available": kali_tools.available_tools(),
                                    "preferred": kali_tools.get_preferred(),
                                    "status": kali_tools.status()})
        if u.path == "/api/experts":
            import experts
            return self._send(200, {"experts": experts.all_experts()})
        if u.path == "/api/bench_questions":
            import model_bench as mb
            return self._send(200, {"questions": mb.load_questions()})
        if u.path == "/api/experts_config":
            # 専門家設定ページ用：全ツールの専門家＋プロバイダ別モデル一覧＋既定
            import experts
            import tools.registry as tr
            from providers import model_catalog as mc
            providers_models = {}
            for p in ["ollama", "groq", "cerebras", "open_router",
                      "google_studio", "mistral"]:
                live = []
                try:
                    live = reg.get_provider(p).list_models()
                except Exception:
                    live = []
                providers_models[p] = {"models": mc.models_for(p, live),
                                       "default": mc.default_model(p)}
            exps = experts.all_experts()
            tools = [t for t in tr.tool_names()
                     if t not in ("expert", "experts_parallel")]
            return self._send(200, {
                "experts": exps,
                "tools": tools,
                "providers_models": providers_models})
        if u.path == "/api/attack_state":
            import engagement
            with engagement.Engagement() as eg:
                return self._send(200, eg.full_state())
        if u.path == "/api/attack_paths":
            import engagement
            with engagement.Engagement() as eg:
                return self._send(200, {"paths": eg.attack_paths()})
        if u.path == "/api/tools_config":
            # ツール管理画面用：全ツールの仕様＋有効状態＋割当て専門家を一括で返す
            import tools.registry as tr
            import experts
            exps = experts.all_experts()
            tools = []
            for s in tr.all_status():
                name = s["name"]
                tools.append({**s, "expert": exps.get(name, {})})
            providers = ["", "ollama", "groq", "cerebras", "open_router",
                         "google_studio", "mistral"]
            return self._send(200, {"tools": tools, "providers": providers})
        if u.path == "/api/tools":
            import tools.registry as tr
            return self._send(200, {"tools": tr.list_specs(only_enabled=False)})
        if u.path == "/api/installs":
            import installs
            return self._send(200, {"installs": installs.installed_list()})
        if u.path == "/api/ssh_status":
            import ssh_session
            return self._send(200, ssh_session.status())
        if u.path == "/api/engines":
            import engine
            return self._send(200, {"engines": engine.engine_status()})
        if u.path == "/api/all_models":
            from providers import model_catalog as mc
            out = {}
            for p in ["ollama", "groq", "cerebras", "open_router",
                      "google_studio", "mistral"]:
                live = []
                try:
                    live = reg.get_provider(p).list_models()
                except Exception:
                    live = []
                out[p] = {"models": mc.models_for(p, live),
                          "default": mc.default_model(p)}
            return self._send(200, {"providers": out})
        if u.path == "/api/models":
            name = parse_qs(u.query).get("provider", [""])[0]
            try:
                models = reg.get_provider(name).list_models()
                return self._send(200, {"models": models})
            except Exception as e:
                return self._send(200, {"models": [], "error": str(e)[:200]})
        if u.path == "/stats":
            with open("web/stats.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/shortterm":
            with open("web/shortterm.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/short_term":
            import agent_loop
            return self._send(200, agent_loop.short_term_snapshot())
        if u.path == "/api/budget":
            import budget
            return self._send(200, budget.snapshot())
        if u.path == "/api/skills":
            from memory import LongTermMemory
            with LongTermMemory() as mem:
                return self._send(200, {"skills": mem.all_skills()})
        if u.path == "/api/lessons":
            from memory import LongTermMemory
            from memory.embed import backend_name
            mem = LongTermMemory()
            try:
                return self._send(200, {"lessons": mem.all_lessons(),
                                        "embed_backend": backend_name()})
            finally:
                mem.close()
        if u.path == "/api/stats":
            import stats
            return self._send(200, {"specialties": stats.model_specialties(),
                                    "recommend": stats.recommend_routes(),
                                    "personality": stats.personality()})
        if u.path == "/api/history":
            import stats
            return self._send(200, {"history": stats.history()})
        if u.path == "/api/modes":
            import modes
            return self._send(200, {"modes": modes.list_modes(),
                                    "current": modes.get_mode()})
        if u.path == "/api/current_run":
            return self._send(200, {"run_id": _LATEST["run_id"]})
        if u.path == "/api/subscribe":
            return self._subscribe(parse_qs(u.query).get("run_id", [""])[0])
        if u.path == "/api/stream":
            return self._stream(parse_qs(u.query).get("run_id", [""])[0])
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/intervene":
            # 実行中のエージェントへ介入メッセージを送る（軌道修正）
            import agent_loop
            msg = (data.get("message") or "").strip()
            if not msg:
                return self._send(200, {"ok": False, "error": "メッセージが空です"})
            agent_loop.push_intervention(msg)
            return self._send(200, {"ok": True})
        if u.path == "/api/stop_agent":
            # 実行中のエージェントを次の区切りで停止
            import agent_loop
            agent_loop.request_stop()
            return self._send(200, {"ok": True})
        if u.path == "/api/engine":
            import engine
            engine.set_required(data["name"], data["required"])
            return self._send(200, {"engines": engine.engine_status()})
        if u.path == "/api/route":
            reg.set_primary(data["role"], data["provider"], data["model"])
            return self._send(200, {"ok": True, "routes": reg.current_routes()})
        if u.path == "/api/api_keys":
            import api_keys
            return self._send(200, {"providers": api_keys.all_config()})
        if u.path == "/api/api_key":
            import api_keys
            provider = data.get("provider", "")
            name = data.get("key_name", "")
            if provider and name:
                api_keys.set_key_name(provider, name)
                # mistralはcontrol側にも反映
                if provider == "mistral":
                    from mistral_llm import mistral_control
                    mistral_control.set_api_key_name(name)
                return self._send(200, {"ok": True, "provider": provider, "key_name": name})
            return self._send(400, {"error": "provider と key_name が必要"})
        if u.path == "/api/mistral_config":
            from mistral_llm import mistral_control
            if data.get("api_key_name"):
                mistral_control.set_api_key_name(data["api_key_name"])
            if "agent_id" in data:
                mistral_control.set_agent_id(data.get("agent_id", ""),
                                             int(data.get("agent_version", 0)))
            return self._send(200, {"ok": True, "config": mistral_control.get_config()})
        if u.path == "/api/consolidate":
            from memory import LongTermMemory
            with LongTermMemory() as mem:
                return self._send(200, mem.consolidate())
        if u.path == "/api/expert":
            import experts
            tool = data.get("tool", "")
            if not tool:
                return self._send(400, {"error": "tool が必要"})
            e = experts.set_expert(tool, data.get("provider"),
                                   data.get("model"), data.get("persona"))
            return self._send(200, {"ok": True, "tool": tool, "expert": e})
        if u.path == "/api/experts_reset":
            import experts
            experts.reset_all()
            return self._send(200, {"ok": True})
        if u.path == "/api/auto_assign_models":
            # 能力ベクトル（実測）から各役割/専門家へ自動割り当て。名前推定は廃止。
            import capabilities as cap
            import router
            import experts
            vectors = cap.all_vectors()
            if not vectors:
                return self._send(200, {"ok": False,
                                        "error": "能力データがありません。先にベンチや実技課題を実行してください。"})
            cands = list(vectors.keys())
            res = router.assign_all(cands)
            for role, key in res["roles"].items():
                if key and role in reg.ROLE_ROUTES:
                    prov, _, mdl = key.partition("/")
                    reg.set_primary(role, prov, mdl)
            for tool, key in res["tools"].items():
                if key:
                    prov, _, mdl = key.partition("/")
                    experts.set_expert(tool, prov, mdl)
            return self._send(200, {"ok": True, "models": cands,
                                    "roles": res["roles"], "tools": res["tools"]})
        if u.path == "/api/benchmark_models":
            # 各モデルに小問を解かせ、実測スコアで役割/専門家を割り当てる。
            # バックグラウンドで実行し、進捗をSSE(/api/subscribe)で配信。run_idを即返す。
            import model_bench as mb
            import experts
            import uuid as _uuid
            sources = data.get("sources")
            questions = None
            if sources:
                questions = mb.build_questions(sources,
                                               per_domain=int(data.get("per_domain", 3)),
                                               test_files=data.get("test_files"))
                mb.save_questions(questions)
            targets = data.get("targets")     # [{"provider","model"},...]
            if not targets:
                try:
                    models = reg.get_provider("ollama").list_models()
                except Exception as ex:
                    return self._send(200, {"ok": False,
                                            "error": f"ollamaモデル取得失敗: {ex}"})
                if not models:
                    return self._send(200, {"ok": False,
                                            "error": "ollamaにモデルがありません"})
                targets = [{"provider": "ollama", "model": m} for m in models]

            run_id = _uuid.uuid4().hex[:12]
            _QUEUES[run_id] = queue.Queue()
            bench_mode = data.get("mode", "normal")   # normal / pentest / both

            def _bench_worker():
                emit = lambda e: _publish(run_id, {"event": "bench",
                                                   "payload": e})
                try:
                    if bench_mode == "both":
                        # 通常とペンテストの両方で実行して拒否差を見る
                        bn = mb.bench_targets(targets, emit=emit,
                                              questions=questions, mode="normal")
                        bp = mb.bench_targets(targets, emit=emit,
                                              questions=questions, mode="pentest")
                        # 割り当てはペンテストモードの結果を採用（実用に近い）
                        bench = bp
                        mb.save_results(bp, normal=bn)
                        cmp = {k: {"normal_refused": bn.get(k, {}).get("total_refused", 0),
                                   "pentest_refused": bp.get(k, {}).get("total_refused", 0)}
                               for k in bp}
                    else:
                        bench = mb.bench_targets(targets, emit=emit,
                                                 questions=questions, mode=bench_mode)
                        mb.save_results(bench)
                        cmp = None
                    res = mb.assign_from_bench(bench)   # 内部で能力ベクトルへ観測も実施
                    for role, key in res["roles"].items():
                        if key and role in reg.ROLE_ROUTES:
                            prov, _, mdl = key.partition("/")
                            reg.set_primary(role, prov, mdl)
                    for tool, key in res["tools"].items():
                        if key:
                            prov, _, mdl = key.partition("/")
                            experts.set_expert(tool, prov, mdl)
                    scores = {k: bench[k]["scores"] for k in bench}
                    speeds = {k: bench[k]["avg_ms"] for k in bench}
                    refused = {k: bench[k].get("total_refused", 0) for k in bench}
                    _publish(run_id, {"event": "bench_result",
                                      "payload": {"ok": True,
                                                  "models": list(bench.keys()),
                                                  "roles": res["roles"],
                                                  "tools": res["tools"],
                                                  "scores": scores,
                                                  "speeds_ms": speeds,
                                                  "refused": refused,
                                                  "compare": cmp}})
                except Exception as ex:
                    _publish(run_id, {"event": "bench_result",
                                      "payload": {"ok": False, "error": str(ex)}})
                finally:
                    _publish(run_id, {"event": "done"})
                    # ストリームを正常終了させる終了シグナル（_streamがNoneで抜ける）
                    q = _QUEUES.get(run_id)
                    if q is not None:
                        q.put(None)
                    # ミラー購読者も終了
                    for sq in list(_SUBS.get(run_id, [])):
                        try:
                            sq.put(None)
                        except Exception:
                            pass

            threading.Thread(target=_bench_worker, daemon=True).start()
            return self._send(200, {"ok": True, "run_id": run_id,
                                    "total": len(targets)})
        if u.path == "/api/bench_questions_save":
            import model_bench as mb
            qs = data.get("questions")
            if not isinstance(qs, dict):
                return self._send(400, {"error": "questions(dict)が必要"})
            mb.save_questions(qs)
            return self._send(200, {"ok": True})
        if u.path == "/api/bench_questions_reset":
            import model_bench as mb
            mb.reset_questions()
            return self._send(200, {"ok": True, "questions": mb.load_questions()})
        if u.path == "/api/ctf_save":
            import ctf_bench as cb
            chs = data.get("challenges")
            if not isinstance(chs, list):
                return self._send(400, {"error": "challenges(list)が必要"})
            cb.save_challenges(chs)
            return self._send(200, {"ok": True})
        if u.path == "/api/ctf_run":
            # 実技課題（フラグ式）をバックグラウンドで実行し、進捗をSSE配信。run_id即返し。
            import ctf_bench as cb
            import experts
            import uuid as _uuid
            challenges = cb.load_challenges()
            if not challenges:
                return self._send(200, {"ok": False, "error": "課題がありません"})
            targets = data.get("targets")
            if not targets:
                try:
                    models = reg.get_provider("ollama").list_models()
                except Exception as ex:
                    return self._send(200, {"ok": False, "error": f"ollamaモデル取得失敗: {ex}"})
                if not models:
                    return self._send(200, {"ok": False, "error": "モデルがありません"})
                targets = [{"provider": "ollama", "model": m} for m in models]

            run_id = _uuid.uuid4().hex[:12]
            _QUEUES[run_id] = queue.Queue()

            def _ctf_worker():
                emit = lambda e: _publish(run_id, {"event": "ctf", "payload": e})
                try:
                    results = cb.run_targets(targets, challenges, emit=emit)
                    assigned = cb.assign_from_ctf(results)
                    # キーは provider/model。分割して専門家へ反映
                    for tool, key in assigned["tools"].items():
                        if key:
                            prov, _, mdl = key.partition("/")
                            experts.set_expert(tool, prov, mdl)
                    summary = {k: {"solved": r["solved"], "total": r["total"],
                                   "avg_s": r["avg_s"]} for k, r in results.items()}
                    _publish(run_id, {"event": "ctf_result",
                                      "payload": {"ok": True, "results": summary,
                                                  "ranking": assigned["ranking"],
                                                  "assigned_tools": assigned["tools"]}})
                except Exception as ex:
                    _publish(run_id, {"event": "ctf_result",
                                      "payload": {"ok": False, "error": str(ex)}})
                finally:
                    _publish(run_id, {"event": "done"})
                    q = _QUEUES.get(run_id)
                    if q is not None:
                        q.put(None)
                    for sq in list(_SUBS.get(run_id, [])):
                        try:
                            sq.put(None)
                        except Exception:
                            pass

            threading.Thread(target=_ctf_worker, daemon=True).start()
            return self._send(200, {"ok": True, "run_id": run_id,
                                    "total": len(targets)})
        if u.path == "/api/bench_questions_generate":
            # LLM生成 or 公開検索で問題を組み立てて保存
            import model_bench as mb
            sources = data.get("sources", ["builtin"])
            qs = mb.build_questions(sources, per_domain=int(data.get("per_domain", 3)))
            mb.save_questions(qs)
            return self._send(200, {"ok": True, "questions": qs})
        if u.path == "/api/engagement_clear":
            import engagement
            with engagement.Engagement() as eg:
                eg.clear()
            return self._send(200, {"ok": True})
        if u.path == "/api/tool_enable":
            import tools.registry as tr
            tool = data.get("tool", "")
            if not tool:
                return self._send(400, {"error": "tool が必要"})
            tr.set_enabled(tool, bool(data.get("enabled", True)))
            return self._send(200, {"ok": True, "tool": tool,
                                    "enabled": tr.is_enabled(tool)})
        if u.path == "/api/mcp_add":
            from tools import mcp_client
            conf = mcp_client.add_server(data.get("name",""), data.get("command",""),
                                         data.get("args",[]))
            return self._send(200, {"ok": True, "servers": conf})
        if u.path == "/api/mcp_remove":
            from tools import mcp_client
            conf = mcp_client.remove_server(data.get("name",""))
            return self._send(200, {"ok": True, "servers": conf})
        if u.path == "/api/kali_refresh":
            import kali_tools
            return self._send(200, kali_tools.refresh(force=True))
        if u.path == "/api/kali_preferred":
            import kali_tools
            kali_tools.set_preferred(data.get("preferred", []))
            return self._send(200, {"ok": True, "preferred": kali_tools.get_preferred()})
        if u.path == "/api/install_remove":
            import installs
            ok = installs.remove(data.get("manager",""), data.get("package",""),
                                 data.get("where","local"))
            return self._send(200, {"ok": ok})
        if u.path == "/api/install_uninstall":
            # 実際にアンインストールコマンドを実行してから記録も削除
            import installs, executor, ssh_session
            mgr = data.get("manager",""); pkg = data.get("package","")
            cmd = installs.uninstall_command(mgr, pkg)
            if ssh_session.is_connected():
                out = ssh_session.run(cmd, timeout=300)
            else:
                out = executor.run_action({"type": "command", "command": cmd},
                                          approver=executor.auto_yes)
            installs.remove(mgr, pkg, data.get("where","local"))
            return self._send(200, {"ok": True, "command": cmd, "output": out[:500]})
        if u.path == "/api/ssh_connect":
            import ssh_session
            r = ssh_session.connect(data.get("host",""), data.get("user",""),
                                    int(data.get("port",22)),
                                    data.get("password"), data.get("key_path"))
            return self._send(200, {"result": r, "status": ssh_session.status()})
        if u.path == "/api/ssh_disconnect":
            import ssh_session
            return self._send(200, {"result": ssh_session.disconnect()})
        if u.path == "/api/apply_recommend":
            import stats
            rec = stats.recommend_routes()
            applied = {}
            for role, items in rec.items():
                if items:
                    reg.set_primary(role, items[0]["provider"], items[0]["model"])
                    applied[role] = [items[0]["provider"], items[0]["model"]]
            return self._send(200, {"applied": applied})
        if u.path == "/api/stats_clear":
            import stats
            stats.clear(); return self._send(200, {"ok": True})
        if u.path == "/api/memory_clear":
            from memory import LongTermMemory
            mem = LongTermMemory()
            try:
                mem.clear(); return self._send(200, {"ok": True})
            finally:
                mem.close()
        if u.path == "/api/mode":
            import modes
            try:
                modes.set_mode(data.get("mode", ""))
                return self._send(200, {"ok": True, "current": modes.get_mode()})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/approve":
            rid = data.get("run_id"); ok = bool(data.get("approved"))
            if rid in _ANSWERS or rid in _QUEUES:
                _ANSWERS.setdefault(rid, queue.Queue()).put(ok)
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "no such run"})
        if u.path == "/api/run":
            import uuid
            if _RUN_ACTIVE["on"]:
                return self._send(409, {"error": "既に別の実行が進行中です。"
                                        "完了を待つか停止してください。"})
            _RUN_ACTIVE["on"] = True
            run_id = uuid.uuid4().hex[:12]
            _QUEUES[run_id] = queue.Queue()
            _ANSWERS[run_id] = queue.Queue()
            _SUBS[run_id] = []
            _LATEST["run_id"] = run_id
            threading.Thread(target=_worker,
                             args=(run_id, data.get("request", ""),
                                   data.get("dry_run", True),
                                   data.get("interactive", False)),
                             daemon=True).start()
            return self._send(200, {"run_id": run_id})
        return self._send(404, {"error": "not found"})

    def _subscribe(self, run_id):
        """mapページ等のミラー購読。主キューを消費せず、配信のコピーを受け取る。"""
        if run_id not in _QUEUES:
            return self._send(404, {"error": "no such run"})
        sq = queue.Queue()
        _SUBS.setdefault(run_id, []).append(sq)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                item = sq.get()
                if item is None:
                    break
                self.wfile.write(f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try: _SUBS.get(run_id, []).remove(sq)
            except ValueError: pass

    def _stream(self, run_id):
        q = _QUEUES.get(run_id)
        if q is None:
            return self._send(404, {"error": "no such run"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        while True:
            item = q.get()
            if item is None:
                break
            try:
                self.wfile.write(f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break        # クライアントが切断した（タブを閉じた等）→ 静かに終了
        _QUEUES.pop(run_id, None)


if __name__ == "__main__":
    # ollamaが未起動なら自動起動を試みる（失敗してもクラウドLLMで継続）
    try:
        import ollamas.ollama_server as _ollama_srv
        import ollamas.ollama_control as _ollama_ctl
        _url = _ollama_ctl._base_url()
        _envh = os.environ.get("OLLAMA_HOST", "")
        print(f"[ollama] 接続先: {_url}/api/tags"
              + (f"（OLLAMA_HOST={_envh} を読み替え）" if _envh else "（既定）"))
        if _ollama_srv.is_ollama_running():
            print("[ollama] 既に起動中です（接続OK）")
        elif _ollama_srv.ensure_ollama():
            print("[ollama] 自動起動に成功しました")
        else:
            print("[ollama] 自動起動できませんでした。"
                  "手動で `ollama serve` を実行するか、クラウドLLMをご利用ください。")
    except Exception as _ex:
        print(f"[ollama] 起動チェック中にエラー: {_ex}")
    print(f"起動: http://{HOST}:{PORT}  (Ctrl+Cで停止)")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
