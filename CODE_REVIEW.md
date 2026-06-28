# LocalAgent — コードレビュー資料

自己ホスト型・自律ペネトレーションテストAIエージェント。
Phase 1〜4.1 のリファクタリングを経た現在の構造をまとめる。

- エントリーポイント: `web_app.py`（Web UI, :8770）/ `web_app2.py`（モデルテスト, :8771）/ `main.py`（CLI）
- オーケストレーター: `agent_loop.py`（`run_agent()`）
- 中核ロジック: `core/` パッケージ（役割クラス＋各エンジン）
- Python 3.10+ / 標準ライブラリ中心、外部依存はすべて遅延import＋フォールバック付き

---

## 1. レイヤー構成（全体像）

```mermaid
flowchart TB
    subgraph Entry["エントリー層"]
        WA["web_app.py :8770<br/>Web UI / SSE"]
        WA2["web_app2.py :8771<br/>モデルベンチ"]
        CLI["main.py CLI"]
    end
    subgraph Orchestrator["オーケストレーション層"]
        AL["agent_loop.py<br/>run_agent()"]
    end
    subgraph Core["core/ 役割・エンジン層"]
        PL["Planner"]
        RS["Researcher"]
        EX["ExecutorRole"]
        CR["Critic"]
        MM["MemoryManager"]
        EE["ExplorationEngine"]
        SE["StrategyEngine"]
        TM["target_manager"]
        EG["execution_guard"]
        EV["evidence_engine"]
        FL["fact_layer"]
        HG["hallucination_guard"]
        WS["WorldState"]
    end
    subgraph Infra["基盤層"]
        PR["providers/<br/>LLMルーティング"]
        EXE["executor.py<br/>コマンド/ファイル実行"]
        LTM["memory/long_term.py<br/>知識グラフ(SQLite)"]
        TOOLS["tools/<br/>recon/exploit/report等"]
    end
    Entry --> AL
    AL --> Core
    AL --> Infra
    Core --> Infra
    PL & RS & CR & MM --> PR
    EX --> EXE
    MM --> LTM
    RS --> LTM
```

---

## 2. core/ クラス図（役割クラスと委譲関係）

役割クラスは既存ロジック（agent_loop/memory/executor）への**ファサード**。
ロジックを再実装せず委譲するため、既存挙動を壊さない設計。

```mermaid
classDiagram
    class Planner {
        +make_goal(request) Goal
        +plan_steps(stm, ltm) list
        +next_action(stm, related, feedback) dict
        +is_empty_action(action) bool
    }
    class Researcher {
        +related_knowledge(query) list
        +active_rules(query) list
        +analyze_observation(obs, world) dict
        -_generate_hypotheses(facts, obs, world) list
    }
    class ExecutorRole {
        +execute(action) str
        +is_failure(result) bool
    }
    class Critic {
        +critique(objective, action, result) dict
        +to_experience(...) Experience
        +objective_done(stm) bool
    }
    class MemoryManager {
        +record_experience(exp)
        +record_critique(objective, goal, critique)
        +distill_rule(objective, critique)
        +reflection_loop() dict
        +target_reflection(world) dict
    }
    Planner ..> Goal : creates
    Researcher ..> WorldState : reads/writes facts
    Critic ..> Experience : creates
    MemoryManager ..> LongTermMemory : persists
    MemoryManager ..> WorldState : target stats
```

### データモデル（core/models.py — dataclass）

```mermaid
classDiagram
    class Goal {
        request
        title
        objectives
        tags
    }
    class Objective {
        description
        status
        tasks
        summary
        success
    }
    class Task {
        description
        status
        actions
        result
    }
    class Action {
        type
        payload
        reason
    }
    class Experience {
        objective
        action
        result
        success
    }
    class Lesson {
        context
        insight
        score
    }
    class Rule {
        condition
        directive
        weight
        confidence
    }
    class Fact {
        type
        name
        value
        confidence
        source
    }
    class Hypothesis {
        description
        confidence
        evidence
        next_steps
        category
    }
    Goal "1" *-- "n" Objective
    Objective "1" *-- "n" Task
    Task "1" *-- "n" Action
    Experience ..> Lesson : abstracts
    Lesson ..> Rule : distills
```

### エンジン群（Phase 3〜4.1）

```mermaid
classDiagram
    class ExplorationEngine {
        +score(hyp) dict
        +peek_hypothesis(hyps) dict
        +select_hypothesis(hyps, commit) dict
        +record_result(hyp, success)
        +is_dead_end(hyp) bool
        +budget_exceeded() bool
        -metrics: dict
    }
    class StrategyEngine {
        +current() dict
        +best_strategy() dict
        +switch_strategy(avoid) dict
        +reorder_hypotheses(hyps) list
        +record_outcome(success)
    }
    class WorldState {
        +add_fact(type,name,value,conf,source)
        +open_hypotheses() list
        +mark_tested(path) / mark_dead_end(path)
        +add_target_node(target,status,...)
        +trusted_targets() / candidate_targets()
        +promote_target(target,...)
        -SQLite: world_state.db
    }
    ExplorationEngine ..> WorldState : tested/dead_end
    StrategyEngine ..> ExplorationEngine : reorder
    ExplorationEngine ..> StrategyEngine : budget→switch
```

---

## 3. 1ターンの制御フロー（ガードの位置）

```mermaid
flowchart TD
    A["Planner.next_action<br/>(plan_with_debate)"] --> B{空 / 重複?}
    B -- yes --> A
    B -- no --> C["Execution Guard<br/>execution_guard.check(action, ctx, world)"]
    C -- "許可外ホスト" --> R1["target_mismatch_blocked<br/>→却下・再計画"]
    C -- ok --> D["Hallucination Guard<br/>事実照合 validate()"]
    D -- "事実と矛盾" --> R2["hallucination_blocked<br/>→却下・再計画"]
    D -- ok --> E["ExecutorRole.execute<br/>(executor.run_action)"]
    E --> F["fact_layer.extract_facts<br/>→ WorldState"]
    F --> G["evidence_engine.extract_evidence<br/>→ trusted/candidate 昇格"]
    G --> H["Critic.critique<br/>成否・原因・改善"]
    H --> I["MemoryManager<br/>経験→教訓→Rule"]
    I --> J["ExplorationEngine.record_result<br/>予算超過→Strategy切替"]
    J --> K{objective達成?}
    K -- no --> A
    K -- yes --> L["Reflection<br/>reflection_loop + target_reflection"]
```

ガードは**実行直前**に2段（Execution Guard → Hallucination Guard）。
LLMがプロンプトを無視しても、許可外ホスト・架空IP・事実矛盾の行動は実行されない。

---

## 4. 使用ライブラリ（すべて遅延import＋フォールバック）

| ライブラリ | 用途 | 使用ファイル | 無い場合 |
|---|---|---|---|
| `ollama` | ローカルLLM・埋め込み（中核） | `ollamas/`, `providers/adapters.py`, `memory/embed.py` | クラウドLLMへ。埋め込みは簡易ベクトルへ |
| `python-dotenv` | .envからAPIキー読込 | `get_env/env_controler.py` | 環境変数直読み |
| `groq` | Groq LLM | `groq_llm/`, `providers/adapters.py` | そのプロバイダのみ無効 |
| `cerebras-cloud-sdk` | Cerebras LLM | `cerebras_llm/`, `providers/adapters.py` | 同上 |
| `openai` | OpenRouter（互換クライアント） | `open_router/` | 同上 |
| `google-genai` | Gemini | `google_studio/` | 同上 |
| `mistralai` | Mistral | `mistral_llm/` | 同上 |
| `paramiko` | SSH永続セッション | `ssh_session.py` | subprocessのsshへ |
| `playwright` | ブラウザ操作・動的Web診断 | `tools/browser.py` | 当該ツールのみ無効 |
| `reportlab` | PDF診断レポート | `tools/report_tool.py` | 当該ツールのみ無効 |
| `search-engines-scraper` | Bing等スクレイプ検索 | `engine/bing.py` 等 | DuckDuckGoエンジンへ |

標準ライブラリのみで動く部分: 記憶DB(SQLite)/統計/Web UI(http.server)/承認/モード/
各種ガード/Fact Layer/Exploration/Strategy/Target管理 — **追加インストール不要**。

外部依存ゼロのもの: Tavily検索（urllibで直接API）。

---

## 5. 永続化（保存先）

### SQLite データベース

| ファイル | モジュール | 内容 |
|---|---|---|
| `memory/agent_memory.db` | `memory/long_term.py` | 知識グラフ: memories/entities/relations/lessons/skills/experiences/rules/plans/strategies/exploration_history/exploration_metrics |
| `world_state.db` | `core/world_state.py` | 世界状態: facts/assumptions/hypotheses/tested_paths/dead_ends/confirmed_findings/executed_targets/rejected_targets/**target_graph**/**target_events** |
| `engagement.db` | `engagement.py` | 攻撃グラフ（サービス/脆弱性） |
| `stats.db` | `stats.py` | 実行統計・トークン消費 |

`world_state.db` は run 単位で生成・クローズ（`_cleanup_run_refs()`）。WALモード。

### JSON 設定・状態ファイル（すべて .gitignore 済み）

| ファイル | 内容 |
|---|---|
| `api_keys.json` | APIキー（機密。git除外） |
| `routes.json` | ROLE_ROUTES（役割→モデル割当） |
| `experts.json` / `capabilities.json` | 専門家設定・能力ベクトル |
| `session.json` | 中断再開用の進捗 |
| `kali_tools.json` / `installed.json` | ツール状態 |
| `bench_questions.json` / `bench_results.json` / `ctf_challenges.json` | ベンチ・CTF |

### 作業ディレクトリ
- `workspace/` — エージェントのファイル生成先（executor が必ずここへ解決。脱出不可）

機密（`.env`, `*api_key*`, `*.key`, `*.pem`, `secrets.json`）と全DB・状態JSONは
`.gitignore` で除外済み。公開リポジトリにはコードとプロンプトのみ含まれる。

---

## 6. LLMルーティング（providers/）

```mermaid
flowchart LR
    Role["role<br/>(plan/judge/summary/<br/>researcher/coder/security/<br/>narrator/explainer)"] --> RR["ROLE_ROUTES<br/>registry.py"]
    RR --> P1["(provider, model) 候補列"]
    P1 --> AD["adapters.py<br/>各プロバイダ実装"]
    AD --> LLM["ollama / groq / cerebras /<br/>gemini / mistral / openrouter"]
```

- `ask(role, msgs)` が role の候補列を順に試行（レート制限時は次候補へ）。
- `ask_direct(provider, model, msgs)` は ROLE_ROUTES を汚さず並列安全。
- 役割ごとに別モデルを割当可能（例: Planner→Gemini / Coder→Qwen / Critic→GPT-OSS）。
  Web UI の役割マップ（/map）から変更でき、`routes.json` に保存。

---

## 7. レビュー時の着目ポイント

1. **ガードの実行順と網羅性** — `agent_loop.py` の実行直前ブロック（Execution Guard →
   Hallucination Guard）。許可外ホスト・架空IPが実行に到達しないことを確認。
2. **Target の唯一信頼源性** — `core/target_manager.py`（不変Context）＋
   `core/world_state.py` の target_graph（証拠で昇格）。LLM推測は candidate 止まり。
3. **証拠抽出の正確さ** — `core/evidence_engine.py` の正規表現と信頼度割当。
4. **経験学習の配線** — Critic→MemoryManager→Rule→Researcher の循環。
   信頼度ゲート（`relevant_rules(min_confidence=0.35)`）。
5. **状態の後始末** — `_cleanup_run_refs()` と run単位リソース解放（接続リーク防止）。
6. **同時実行制御** — `web_app.py` の `_RUN_ACTIVE`（メインrunは1本に制限）。
7. **フォールバック設計** — 全 core 参照が try/except 内。エンジン不在時は従来動作へ。

各 Phase の詳細設計は `ARCHITECTURE.md`〜`ARCHITECTURE_PHASE4.md` を参照。
