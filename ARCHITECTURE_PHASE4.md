# LocalAgent — Mythos Phase 4.1（証拠に基づくターゲット管理）

ターゲットを「固定値」でも「自由変更」でもなく、証拠付きグラフ(Evidence Graph)
として管理する。LLMの推測では増やさず、実際の観測結果からのみ拡張する。

## 動作の流れ
```
User Target → Target Manager → (run中) Observation → Evidence Engine
   → Target Graph(trusted/candidate) → Execution Guard → Executor
```

## 1. Target Manager（core/target_manager.py）
Primary Target を解析・正規化し、不変の Target Context を生成（run開始時に1度）。
allowed_hosts / allowed_domains / allowed_networks(/24) を導出。host_allowed() で照合。

## 2. Target Graph（World State内）
ツリーでなくグラフ。各ノードは status(trusted/candidate/rejected) と provenance を持つ。
- trusted: 実行可能（primary + 証拠で昇格したホスト）
- candidate: 要証拠・実行不可（LLM提案など）
- rejected: 却下履歴

## 3. Evidence Engine（core/evidence_engine.py）
観測テキスト（実ツール出力）から証拠付きで新ターゲットを抽出。対応源と信頼度:
DNS/Reverse DNS/HTTP Redirect/Location 1.00、Nmap 1.00、SSL SAN/CN 0.95、
robots.txt/sitemap 0.90、HTML Link 0.85、JavaScript 0.80、WHOIS 0.75、LLM 0.00。
信頼度 ≥ 0.75（TRUST_THRESHOLD）で trusted へ昇格、未満は candidate。

## 4. LLM Expansion 禁止
LLMの推測だけではターゲットを追加・実行できない。candidate として保持はできるが、
偵察で実在を観測し証拠を得るまで Executor は実行しない。

## 5. Provenance（来歴）
全ターゲットに source / parent / evidence / confidence / timestamp を保持。
「なぜ追加されたか」を完全追跡可能。target_events に履歴も記録。

## 6. Confidence
証拠源ごとの既定信頼度を SOURCE_CONFIDENCE で定義。World State に保存。

## 7. Execution Guard 改修（core/execution_guard.py）
check(action, ctx, world) が、不変ctx(primary)に加えて World State の
trusted_targets も許可対象に含めて照合。trusted に無いホストは Reject。

## 8. Researcher 制約
新ターゲットは candidate として提案可。next_steps/command には primary または
trusted のみ使用。証拠が得られるまで candidate は実行されない（プロンプトで明示）。

## 9. World State 変更（core/world_state.py）
追加テーブル: target_graph(target,status,source,parent,evidence,confidence)、
target_events。メソッド: add_target_node / trusted_targets / candidate_targets /
trusted_target_names / promote_target / target_graph / target_events。

## 10. Reflection 変更（core/memory_manager.py）
target_reflection(world): 拡張成功数・源(source)別集計・却下数を分析。
同じ許可外ホストへ繰り返しアクセスを試みた場合は教訓/ルール化。

## agent_loop 変更箇所
- run開始: build_context→freeze→主ターゲットを trusted グラフに登録。
- 観測後: evidence_engine で証拠抽出→trusted/candidate へ追加（target_expandedイベント）。
- 実行直前: execution_guard.check(action,ctx,world) で trusted 照合、許可外は却下。
- 実行後: 実行ホストを executed_targets に記録。
- run終了: target_reflection で拡張/却下を統計化。
- planner文脈: trusted/candidate を注入（候補は要証拠・未実行と明示）。
- 新イベント: target_locked/target_unresolved/target_mismatch_blocked/
  target_expanded/target_reflection。

## 移行手順
1. 既存DB/world_state.dbはそのまま使用可（新テーブルは起動時に自動作成）。
2. 攻撃的モードで run すると主ターゲットが trusted 起点になり、偵察で自動拡張。
3. ロールバックは evidence_engine.py 除外＋agent_loop差分を戻すのみ
   （全参照は try/except 内、無くても従来動作にフォールバック）。

## 完全互換を維持したもの
providers/router/benchmark/memory/WebUI/phase_readiness/capability routing/
intervention/killchain/Fact Layer/World State/Exploration Engine/Strategy Engine
— すべて無改変で動作。Phase4.1は攻撃的モードでのみ有効化。

## 設計の要点
- ターゲットを勝手に変更しない（LLM推測は実行不可のcandidate止まり）
- 実際の観測結果から論理的に拡張できる（HTTP Location等で自動trusted昇格）
- 全ターゲットに証拠(Provenance)を保持し、安全性・追跡可能性・柔軟性を両立
