# LocalAgent — Mythos Phase 2（仮説駆動型）

観測事実に基づいて仮説を生成し、推測と事実を厳密に区別するエージェントへ進化。
目的は「攻撃性の強化」ではなく「事実準拠の規律ある推論」。

## 動作ループ
```
Observation → Fact抽出 → World Stateへ保存 → Hypothesis生成(≥3)
   → Investigation → Experiment → Result
   → 事実照合(Hallucination Guard) → Critic → Lesson → Rule(信頼度付き)
   → Reflection（弱いRule抑制・強いRule強化）
```

## 1. 問題になっていた箇所
- 調査が nmap→nikto の固定パターン化（Observationから仮説を作れていない）。
- 事実と推測の混同（Apacheのみ観測 なのに Nginx調査を提案＝幻覚）。

## 2. Fact Layer（core/fact_layer.py）
観測テキストから事実を正規表現で決定論的に抽出（LLM不使用＝幻覚ゼロ）。
service/version/port/cve/endpoint/os/credential を {type,name,value,confidence,source} で出力。
観測由来は confidence=1.0、推定混じりは低く設定。

## 3. Hallucination Prevention（core/hallucination_guard.py）
行動実行前に World State の事実と矛盾しないか検証。
- 事実=Apache のみ → 行動「Nginx CVE調査」→ **Reject**（同カテゴリの未確認すり替えを検出）
- 事実=Apache → 行動「Apache CVE調査」→ OK
- 事実がまだ無い偵察初期は素通し（誤検出回避のため保守的判定）。

## 4. World State（core/world_state.py）
事実/推測/仮説/試行済み経路/行き止まり/確定発見を SQLite で永続管理。
`snapshot()` が要件9の形式（services/versions/facts/assumptions/hypotheses/
tested_paths/confirmed_findings/dead_ends）を返す。プランナーへ要約注入。

## 5. Researcher 改修（core/researcher.py）
`analyze_observation()` が観測を facts / assumptions / hypotheses に分類し、
事実に基づく検証可能な仮説を**最低3件**生成。存在しないサービスの仮説は作らない。

## 6. Rule Engine 改修（memory/long_term.py の rules）
列追加: confidence / success_rate / successes / failures / last_verified / priority。
- `record_rule_outcome()` が適用ごとに成否を記録し、ラプラス平滑化で信頼度更新。
- `relevant_rules(min_confidence=0.35)` が低信頼ルールを自動適用から除外。
- 旧DBは `_ensure_rule_columns()` が ALTER TABLE で自動マイグレーション。

## 7. Reflection 改修（core/memory_manager.py）
繰り返す失敗目的を新ルール化、効果の低い/高いルールを集計（弱→自動適用外、強→温存）。
記憶統合（重複削除・教訓→スキル昇華・剪定）も実施。run終了時に自動実行。

## 8. クラス図（要点）
```
WorldState ──facts/hypotheses──┐
Researcher.analyze_observation ─┤→ Planner(事実+仮説+ルールを文脈に) → Action
fact_layer.extract_facts ───────┘                                        │
hallucination_guard.validate ◀──────── 実行前に事実照合（Reject可）◀──────┘
Critic → MemoryManager(record_experience/critique/rule outcome) → Rule(信頼度)
MemoryManager.reflection_loop ── 弱Rule抑制/強Rule強化/新Rule
```

## 9. DBスキーマ変更
- rules: +confidence,+success_rate,+successes,+failures,+last_verified,+priority
- world_state.db（新規）: facts/assumptions/hypotheses/tested_paths/dead_ends/confirmed_findings

## 10〜11. 実装コード/差分
新規: core/{fact_layer,world_state,hallucination_guard}.py、models.py(+Fact/Assumption/Hypothesis)。
改修: core/researcher.py(仮説エンジン)、core/memory_manager.py(Reflection強化)、
memory/long_term.py(Rule Engine強化)、agent_loop.py(事実抽出・事実照合・ルール成否・世界状態注入)。

## 12. 移行手順
1. 既存DBはそのまま使用可（rules列は起動時に自動マイグレーション）。
2. 攻撃的モード(pentest/recon/killchain)で run すると world_state.db が自動生成。
3. 既存の routes.json / capabilities.json / experts.json はそのまま有効。
4. ロールバックは core/ の新規3ファイルを除外し agent_loop の差分を戻すだけ
   （World参照は try/except で囲ってあるため、無くても既存動作に影響しない）。

## 完全互換を維持したもの
providers / router / benchmark / memory / WebUI / phase_readiness /
capability routing / intervention system / killchain mode — すべて無改変で動作。
Phase2機能は攻撃的モードでのみ有効化され、World参照が無い場合は従来挙動に自動フォールバック。
