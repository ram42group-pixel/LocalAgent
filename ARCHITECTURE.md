# LocalAgent — Mythos アーキテクチャ

長期自律・自己改善・経験学習・マルチエージェント協調を志向した役割分離アーキテクチャ。
既存機能（providers/router/memory/benchmark/WebUI）は一切壊さず、構造を進化させた。

## 1. 解決した問題点
- `agent_loop.py` に責務が集中（計画・実行・批評・記憶・学習が混在、1000行超）。
- 経験学習が限定的（失敗を会話履歴として持つだけで、再利用可能な規範に昇華しない）。
- 目標が Goal→Objective の2層のみで、大規模タスクの分解余地がない。
- 批評（成否判定・原因分析・改善案）が散在し、長期記憶に体系的に残らない。
- 役割ごとに別モデルを割り当てる構造が未整備。

## 2. 新アーキテクチャ

```
        ┌────────────── agent_loop (オーケストレーター) ──────────────┐
        │                                                             │
   Researcher → Planner → ExecutorRole → Critic → MemoryManager      │
   (知識収集)   (計画立案)   (実行)        (批評)     (記憶/学習)        │
        ▲                                              │              │
        └──────────── 自動適用ルールを次の計画へ注入 ◀──┘              │
                                                                      │
   Reflectionループ: 最近の失敗/成功を振り返り、繰り返す失敗をルール化  │
        └─────────────────────────────────────────────────────────────┘
```

役割ごとに providers の routing role を割り当て可能（役割マップ /map から差替）:
Planner→plan / Researcher→researcher / Coder→coder / Critic→judge / SecurityExpert→security

## 3. ディレクトリ構成（追加分）
```
core/
  __init__.py          役割クラスの公開・マルチエージェント役割登録
  models.py            Goal/Objective/Task/Action, Experience/Lesson/Rule
  planner.py           Planner（計画立案。agent_loopへ委譲）
  researcher.py        Researcher（知識/経験/ルール検索）
  executor_role.py     ExecutorRole（実行。executorへ委譲）
  critic.py            Critic（成否判定・原因分析・改善案）
  memory_manager.py    MemoryManager（経験→教訓→ルール昇華・Reflection）
  roles.py             アーキ役割→ルーティングrole の対応・登録
```

## 4. クラス設計（要点）
- 各役割クラスは既存ロジック（agent_loop/memory/executor）への**ファサード**。
  ロジックを再実装しないため挙動は不変。構造だけがクリーンになる。
- `Critic.critique()` → {success, cause, improvement, directive}
- `MemoryManager.record_critique()` → 失敗は教訓+ルール化、成功は強化教訓。
- `MemoryManager.reflection_loop()` → 繰り返す失敗をルール化＋記憶統合。

## 5. データモデル（KG拡張）
既存 memories/entities/relations/lessons/skills に加え:
- `experiences` 1試行の生データ（objective, action, result, success, vec）
- `rules`       自動適用ルール（condition, directive, weight, uses）
- `plans`       計画の保存（goal, objective, steps, status）

経験学習: Experience（生）→ Lesson（教訓）→ Rule（自動適用）。
Rule は `relevant_rules()` で次回計画の文脈へ自動注入される。

## 6. 実装優先順位（完了済み）
1. ✅ データモデル（models.py）と KG拡張（experiences/rules/plans）
2. ✅ 役割クラス（Planner/Researcher/Executor/Critic/MemoryManager）
3. ✅ Critic→経験/教訓/ルール記録の配線（loop内）
4. ✅ ルールの計画文脈への自動注入
5. ✅ Reflectionループ（run終了時の自己改善）
6. ✅ マルチエージェント役割登録（researcher/coder/security）

## 7. 既存機能の維持
providers / router / memory / benchmark / WebUI / 介入機能 / 空プランガード /
phase_readiness / killchainモード / capabilities すべて生存・無改変で動作。
agent_loop の関数群は互換のため残置（役割クラスが委譲先として使用）。
