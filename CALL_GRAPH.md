# LocalAgent — クラス・関数の呼び出し関係図

実コードから抽出した呼び出し関係。`agent_loop.run_agent()` を起点に、
役割クラス→委譲先、各ガード、LLM呼び出し、永続化までを追える。

---

## 1. 全体の呼び出しフロー（エントリー → run_agent → 各層）

```mermaid
flowchart TD
    UI["web_app.py<br/>do_POST /api/run"] --> W["_worker()"]
    W --> RA["agent_loop.run_agent()"]
    CLI["main.py"] --> RA

    RA --> MG["make_goal()"]
    RA --> TM["core.target_manager<br/>.build_context / .freeze"]
    RA --> WSnew["core.WorldState()"]
    RA --> SEnew["core.StrategyEngine()"]

    RA --> LOOP{{"objectiveループ<br/>while not stm.finished"}}
    LOOP --> PS["plan_steps()"]
    LOOP --> EEnew["core.ExplorationEngine()"]
    LOOP --> TURN{{"turnループ<br/>while turn < max"}}

    TURN --> PWD["plan_with_debate()"]
    TURN --> EG["core.execution_guard.check()"]
    TURN --> HG["core.hallucination_guard.validate()"]
    TURN --> EA["execute_action()"]
    TURN --> OBS["観測処理<br/>fact / evidence / critic"]
    TURN --> ID["is_objective_done()"]

    LOOP --> SO["summarize_objective()"]
    LOOP --> RL["reflect_and_learn()"]
    RA --> REFL["MemoryManager.reflection_loop()<br/>+ target_reflection()"]
    RA --> CU["_cleanup_run_refs()"]
```

---

## 2. 計画フェーズ: plan_with_debate / plan_next_action の呼び出し

```mermaid
flowchart TD
    PWD["plan_with_debate()"] --> PNA["plan_next_action()"]
    PWD --> JUDGE["ask_role('judge')<br/>→ providers.ask"]

    subgraph PNA_body["plan_next_action() の内部"]
        PNA --> RK["ltm.related_knowledge()"]
        PNA --> RLes["ltm.relevant_lessons()"]
        PNA --> RR["ltm.relevant_rules(min_conf=0.35)"]
        PNA --> MRU["ltm.mark_rule_used()"]
        PNA --> SS["ltm.semantic_search()"]
        PNA --> RSk["ltm.relevant_skills()"]
        PNA --> WSp["_world.prompt_text()"]
        PNA --> OH["_world.open_hypotheses()"]
        PNA --> RH["strat.reorder_hypotheses()"]
        PNA --> PK["eng.peek_hypothesis()"]
        PNA --> BC["stm.build_context()"]
        PNA --> ASK["ask_role('plan')"]
    end
    ASK --> AR["_ask_role_inner()"]
```

---

## 3. LLM呼び出しの中核: ask_role → providers

```mermaid
flowchart TD
    AR["_ask_role_inner()"] --> ASK["providers.ask(role, msgs)"]
    AR --> H["handler()<br/>(JSON検証)"]
    AR --> RJ["repair_json()"]
    AR --> RT["_retry_local()<br/>(ollamaフォールバック)"]
    AR --> LR["_looks_like_refusal()"]

    ASK --> REG["providers.registry"]
    REG --> RR2["ROLE_ROUTES[role]<br/>候補(provider,model)列"]
    REG --> AD["providers.adapters"]
    AD --> O["ollama"]
    AD --> G["groq"]
    AD --> C["cerebras"]
    AD --> GM["gemini"]
    AD --> MI["mistral"]
    AD --> ORr["openrouter"]
    RT --> O
```

---

## 4. 役割クラス（core/）→ 委譲先（ファサード構造）

役割クラスはロジックを再実装せず、agent_loop / memory / executor へ委譲する。

```mermaid
flowchart LR
    subgraph Roles["core/ 役割クラス"]
        PL["Planner"]
        RS["Researcher"]
        EX["ExecutorRole"]
        CR["Critic"]
        MM["MemoryManager"]
    end

    PL -->|make_goal| ALmg["agent_loop.make_goal"]
    PL -->|plan_steps| ALps["agent_loop.plan_steps"]
    PL -->|next_action| ALpwd["agent_loop.plan_with_debate"]

    RS -->|related_knowledge| LTM1["ltm.related_knowledge"]
    RS -->|active_rules| LTM2["ltm.relevant_rules"]
    RS -->|analyze_observation| FL["fact_layer.extract_facts"]
    RS -->|_generate_hypotheses| WS1["world.add_hypothesis"]

    EX -->|execute| ALea["agent_loop.execute_action"]
    ALea --> RUN["executor.run_action"]

    CR -->|critique| RP["replan.analyze"]
    CR -->|objective_done| ALid["agent_loop.is_objective_done"]

    MM -->|record_experience| LTM3["ltm.add_experience"]
    MM -->|distill_rule| LTM4["ltm.add_rule"]
    MM -->|reflect| ALrl["agent_loop.reflect_and_learn"]
    MM -->|consolidate| CONS["consolidation.run_full"]
```

---

## 5. 実行フェーズ: execute_action → executor ディスパッチ

```mermaid
flowchart TD
    EA["agent_loop.execute_action()"] --> RUN["executor.run_action()"]
    RUN --> NORM["_normalize_action_type()<br/>create→file/write 等"]
    RUN --> DISP{{"_DISPATCH[type]"}}
    DISP --> CMD["_run_command<br/>(shell)"]
    DISP --> FILE["_run_file<br/>(_resolve→workspace/)"]
    DISP --> CODE["_run_code"]
    DISP --> WS["_run_web_search"]
    DISP --> SSH["_run_ssh_connect"]
    DISP --> TOOL["_run_tool<br/>→ tools/registry"]
    DISP --> ASSIST["_run_assist"]
```

---

## 6. 観測後の処理: 事実抽出 → 証拠拡張 → 批評 → 学習

実行結果(result)を受けて、5つの処理が順に走る。

```mermaid
flowchart TD
    RES["execute_action の result"] --> FE["Researcher.analyze_observation()"]
    FE --> EF["fact_layer.extract_facts()"]
    EF --> WAF["world.add_facts()"]
    FE --> GH["_generate_hypotheses()"]
    GH --> WAH["world.add_hypothesis()"]

    RES --> EV["evidence_engine.extract_evidence()"]
    EV --> ATN["world.add_target_node()<br/>trusted / candidate 昇格"]

    RES --> CRI["Critic.critique()"]
    CRI --> ISF["is_failure_result()"]
    CRI --> RPA["replan.analyze()"]

    CRI --> MMR["MemoryManager"]
    MMR --> REX["record_experience → ltm.add_experience"]
    MMR --> RCR["record_critique → ltm.add_lesson / add_rule"]
    MMR --> RRO["ltm.record_rule_outcome()<br/>(適用ルールの成否)"]

    RES --> ER["ExplorationEngine.record_result()"]
    ER --> WMT["world.mark_tested / mark_dead_end"]
    ER --> BE{{"budget_exceeded?"}}
    BE -->|yes| SW["StrategyEngine.switch_strategy()"]
    ER --> ST{{"stuck?"}}
    ST -->|yes| REGEN["Researcher.analyze_observation<br/>(仮説再生成)"]
```

---

## 7. ガードの呼び出し順（実行直前の防御）

```mermaid
flowchart TD
    ACT["生成された action"] --> Q1{{"_is_empty_action?"}}
    Q1 -->|空| RETRY1["再計画"]
    Q1 -->|no| Q2{{"dedup.is_duplicate?"}}
    Q2 -->|重複| RETRY2["再計画"]
    Q2 -->|no| G1["execution_guard.check<br/>(action, ctx, world)"]
    G1 -->|許可外ホスト| BLK1["target_mismatch_blocked"]
    G1 -->|ctx無時| IPG["hallucination_guard.validate_ip"]
    IPG -->|架空IP| BLK2["ip_guess_blocked"]
    G1 -->|ok| G2["hallucination_guard.validate<br/>(事実照合)"]
    G2 -->|事実矛盾| BLK3["hallucination_blocked"]
    G2 -->|ok| EXEC["execute_action()"]
```

---

## 8. 永続化の呼び出し先（どの処理がどのDBに書くか）

```mermaid
flowchart LR
    subgraph Writers["書き込む処理"]
        MM["MemoryManager"]
        RS["Researcher / fact層"]
        EV["evidence_engine 経由"]
        EE["ExplorationEngine 経由"]
        SE["StrategyEngine"]
    end
    subgraph DBs["SQLite"]
        AM[("agent_memory.db<br/>experiences/rules/lessons<br/>strategies/exploration_*")]
        WSD[("world_state.db<br/>facts/hypotheses<br/>target_graph/target_events")]
    end
    MM --> AM
    SE --> AM
    EE --> AM
    RS --> WSD
    EV --> WSD
```

---

## 凡例・補足

- `ask_role('plan' / 'judge' / ...)` は `providers.ask(role, msgs)` を介して
  `ROLE_ROUTES` の候補(provider,model)列を順に試す（レート制限時は次候補へ）。
- 役割クラス（Planner等）は薄いファサード。実ロジックは委譲先（agent_loop の関数群、
  memory、executor）にあり、これにより既存挙動を壊さず構造を分離している。
- ガードはすべて **execute_action の直前** に集約（図7）。LLMがプロンプトを無視しても、
  許可外ホスト・架空IP・事実矛盾の行動は実行に到達しない。
- 図はレビュー用に層ごとに分割。詳細な責務は `CODE_REVIEW.md`、
  各Phase設計は `ARCHITECTURE*.md` を参照。
