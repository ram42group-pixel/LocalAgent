# LocalAgent — Mythos Phase 4.2（Evidence-backed Scope Graph）

Phase4.1 の Target Graph を Scope Graph へ進化させた。管理するのは
「ターゲット一覧」ではなく「Root から証拠を経てどう到達したか」という
探索経路(Evidence Chain) と 関係(Relation)。

## 設計思想
- Scope は固定でなく、Evidence によって成長するグラフ。
- LLM は Scope を変更できない。Evidence のみが Scope を拡張する。
- Evidence Chain を持たない Target は自動実行対象にしない。

## 構造
Root Target -> Observation -> Evidence -> Scope Graph
  -> Planner -> Researcher -> Exploration -> Execution Guard -> Executor

## Scope Graph（World State: target_graph）
各ノード: target / parent / relation / evidence / confidence /
discovery_time(created_at) / status / is_root。
各エッジ(relation): resolves_to / redirects_to / links_to / references /
certificate / dns / api / smb / ldap。
証拠源->relation の対応は evidence_engine.SOURCE_RELATION。

## Evidence Chain（中核）
world_state.evidence_chain(target) が target から parent を辿って Root まで
の経路を返す。is_reachable(target) は Root に到達でき、経路上に rejected が
無いことを検証する。chain_explanation(target) が「なぜここを探索しているか」
を人間可読で返す。
例: admin.xxx.com <-[links_to]- login.xxx.com <-[redirects_to]- xxx.com(root)

## Execution Guard（経路検証）
execution_guard.check(action, ctx, world) は、コマンド内の各ホストについて
world.reachable_targets()（Root から証拠経路で到達可能な集合）に含まれるかを
検証する。primary/ctx 許可 or 到達可能 以外は Reject。
- xxx.com->DNS->api->redirect->login->HTML->admin は実行可能。
- xxx.com->LLM推測->localhost は Evidence Chain が無いため Reject。

## Reflection（関係の有効性評価）
memory_manager.target_reflection(world) が拡張/却下に加え、
relation 別の有効性を評価する。到達可能ノードを生んだ関係を useful、
rejected/到達不能を生んだ関係を ineffective として集計。誤誘導の多い関係は
教訓化して信頼を下げる。到達可能ターゲットの Evidence Chain も記録。

## agent_loop 変更
- run開始: 主ターゲットを is_root=True で Scope Graph に登録。
- 観測後: evidence_engine で抽出->relation 付きで add_target_node。親は
  「その観測を生んだ到達可能ホスト」にして Evidence Chain を正しく伸ばす。
- 実行直前: execution_guard が reachable_targets で経路検証->許可外は却下。
- planner文脈: 到達可能ターゲットを Evidence Chain 付きで提示。
- run終了: target_reflection で relation の有効性を学習。

## World State 変更
target_graph に relation / is_root 列を追加（旧DBは起動時に自動マイグレーション）。
新メソッド: evidence_chain / is_reachable / reachable_targets / chain_explanation。

## 移行手順
1. 旧 world_state.db はそのまま使用可（relation/is_root 列を自動追加）。
2. 攻撃的モードで run すると Root が is_root で登録され、偵察で経路が伸びる。
3. ロールバックは agent_loop の Phase4.2 差分を戻すのみ（全参照 try/except 内）。

## 完全互換
providers/router/benchmark/memory/WebUI/phase_readiness/capability routing/
intervention/killchain/Fact Layer/World State/Exploration/Strategy はすべて無改変。
Phase4.2 は攻撃的モードでのみ有効。is_reachable 不在の旧Worldでは trusted で代替。

## 要点
- Target ではなく Target 間の Relation を管理する。
- 全 Target が「なぜここを探索しているか」を Evidence Chain で説明可能。
- Evidence Chain を持たない Target は自動実行されない。
