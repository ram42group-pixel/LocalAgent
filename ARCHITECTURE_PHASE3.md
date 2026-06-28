# LocalAgent — Mythos Phase 3（探索駆動型）

Fact駆動からExploration駆動へ。同じ失敗経路に固執せず、新規性・多様性を
取り込んで探索空間を広げる自律エージェント。目的は攻撃性向上ではなく、
「Fact → Hypothesis → Exploration → Learning」を回し局所最適化を避けること。

## 動作ループ
```
Observation → Fact抽出 → World State → Hypothesis生成
   → Exploration Engine（仮説スコアリング・選択）
   → Investigation Plan → Experiment → Result
   → Critic → Lesson → Rule → Reflection（Rule＋Strategy評価）
```

## 1. 探索アルゴリズムの問題点
- 局所最適化: 高信頼仮説(Apache CVE)に固執し、失敗派生を繰り返す。
- 探索済み経路の再試行: World Stateに履歴はあるが活用不足。
- 多様性不足: 高信頼ばかり選び、低信頼だが有望な仮説を試さない。

## 2. Exploration Engine（core/exploration_engine.py）
仮説を confidence だけで選ばず、総合スコアで選択:
```
score = confidence + novelty_bonus(0.4) + diversity_bonus(0.3) - repetition_penalty(0.25×失敗回数)
```
- novelty: 未試行の経路に加点。
- diversity: 直近3手と異なるカテゴリに加点／同一連続には減点。
- repetition: 同経路の失敗回数に応じ減点（World Stateのtested/dead_endも加味）。
- dead-end判定: 同一経路3回失敗で行き止まり化＝候補から除外。
- 予算管理: max_attempts=20 / max_dead_ends=5 を超えると戦略切替。
- メトリクス: exploration_depth/unique_hypotheses/dead_ends/novel_paths/strategy_switches。

## 3. Strategy Engine（core/strategy_engine.py）
Ruleの上位概念。複数Ruleを束ねた探索方針（カテゴリ優先順）を持つ。
既定戦略: known_cve_first / web_surface_first / misconfig_first / auth_first。
- best_strategy(): 成功率最大の戦略を選択（未経験は楽観的初期化で探索促進）。
- reorder_hypotheses(): 戦略のカテゴリ優先順で仮説を並べ替え。
- switch_strategy(): 成果が出ない時に別戦略へ。
- record_outcome(): 成否を記録し成功率を更新（永続化）。
階層: Rule（個別規範）< Policy（条件付き方針）< Strategy（探索方針＝複数Rule束ね）。

## 4. クラス図（要点）
```
World State ──open_hypotheses──▶ Strategy.reorder ──▶ Exploration.select_hypothesis
                                                              │（score: conf+nov+div-rep）
Planner ◀── 推奨仮説を文脈注入 ◀──────────────────────────────┘
   │
   ▼ Action → Executor → Result → Critic
                                    │
              Exploration.record_result / Strategy.record_outcome
                                    │
              予算超過→switch_strategy / stuck→Researcher.再生成
                                    │
              Reflection: Rule評価＋Strategy評価（強化/弱体化）
```

## 5. DBスキーマ変更（memory/long_term.py）
- strategies(name,description,success_rate,successes,failures,uses,last_used)
- exploration_history(objective,hypothesis,category,score,success)
- exploration_metrics(objective,exploration_depth,unique_hypotheses,dead_ends,novel_paths,strategy_switches)

## 6. agent_loop変更箇所
- run開始: StrategyEngine生成（run単位）。
- objective開始: ExplorationEngine生成（objective単位）。
- 計画文脈: 戦略で並べ替えた仮説をExplorationが選び、推奨仮説＋戦略を注入。
- 結果後: Exploration.record_result / Strategy.record_outcome / 探索履歴記録。
        予算超過→戦略切替、stuck→仮説再生成。
- objective終了: 探索メトリクスを永続化。
- run終了: Reflectionで戦略も評価。新イベント: strategy_switch/dead_end_detected/exploration_metrics。

## 7〜8. 実装コード/差分パッチ
新規: core/exploration_engine.py, core/strategy_engine.py。
改修: core/models.py(+category), core/memory_manager.py(Reflection強化),
core/__init__.py(export), memory/long_term.py(3テーブル＋メソッド), agent_loop.py(配線)。

## 9. 移行手順
1. 既存DBはそのまま使用可（新テーブルは起動時に自動作成）。
2. 攻撃的モードで run すると Exploration/Strategy が自動稼働。
3. ロールバックは新規2ファイル除外＋agent_loop差分戻しのみ
   （全参照は try/except 内で、無い場合は従来挙動に自動フォールバック）。

## 完全互換を維持したもの
providers / router / benchmark / memory / WebUI / phase_readiness /
capability routing / intervention / killchain / Phase2 Fact Layer — すべて無改変で動作。
Phase3は攻撃的モードでのみ有効化され、エンジン不在時は従来の計画ロジックに戻る。
