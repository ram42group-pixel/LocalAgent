# LocalAgent

自己ホスト型の自律ペネトレーションテスト AI エージェント。複数の LLM プロバイダ
（ローカルの ollama ＋ クラウド各社）を役割ごとに使い分け、偵察から実証
（exploit）・権限昇格・横展開・レポート生成までを自律的に進めます。

> A self-hosted autonomous penetration-testing AI agent. It orchestrates multiple
> LLM providers (local ollama + several cloud providers) by role and works
> autonomously from reconnaissance through exploitation, privilege escalation,
> lateral movement, and reporting.

HackingBuddyGPT / Strix / AutoPentester に着想を得ています。
Inspired by HackingBuddyGPT, Strix, and AutoPentester.

---

## ⚠️ 法的・倫理的な注意 / Legal & Ethical Notice

**このツールは、あなた自身が所有するシステム、または明示的かつ書面で許可を得た
システムに対するセキュリティ診断のためだけに使用してください。**

無許可のコンピュータへの侵入・アクセスは、日本の不正アクセス禁止法をはじめ、
ほとんどの国・地域で**犯罪**です。本ツールは実際に脆弱性を突く（exploit する）
機能を含みます。利用者は、対象システムに対する正当な権限を持つことを自ら確認する
責任を負います。

**Use this tool ONLY against systems you own or are explicitly authorized (in
writing) to test.** Unauthorized access to computer systems is a **crime** in most
jurisdictions. This tool can perform real exploitation. You are solely responsible
for ensuring you have proper authorization for any target.

作者および貢献者は、本ソフトウェアの誤用・違法な使用について一切の責任を負いません。
本ソフトウェアは Apache License 2.0 の下で「現状のまま（AS IS）」提供され、いかなる
保証もありません。

The authors and contributors assume no liability for misuse or any illegal use.
This software is provided "AS IS" under the Apache License 2.0, without warranty
of any kind.

---

## 主な機能 / Features

- **多プロバイダ・オーケストレーション** — 役割（計画 / 目標設計 / 判定 / 要約）ごとに
  最適な LLM を割り当て。クラウドを主軸にしつつ、生成拒否時はローカル ollama へ自動
  フォールバック。
- **完全自律の攻撃チェーン** — 偵察 → 列挙 → CVE 照合 → exploit → 権限昇格 → 横展開 →
  日本語レポート（PDF）を一連で実行。
- **構造化された攻撃グラフ** — ホスト / サービス / 脆弱性 / 認証情報 / 発見事項を
  インベントリ化し、攻撃経路の推論に利用。
- **ReAct 的な戦略立案** — 複数の攻撃仮説を立て、成功可能性 × インパクト ÷ コストで
  採点して有望な経路を選択。
- **モデル評価（ベンチマーク）** — 各モデルに学科問題（推論 / コード / セキュリティ /
  速度）や実技課題（フラグ式）を解かせ、実測スコアで役割へ自動割り当て。
- **Web UI** — コンソール、攻撃グラフ、記憶グラフ、ツール管理、専門家設定、モデル評価。

---

## 動作環境 / Requirements

- Python 3.11+（開発は 3.14 / developed on 3.14）
- [ollama](https://ollama.com/)（ローカル LLM 用。クラウドのみで使う場合は任意）
- 任意: Kali Linux のツール群（nmap, Metasploit, sqlmap, crackmapexec など）。
  実際の診断にはこれらが対象環境側または実行ホストに必要です。
- 任意: 各クラウド LLM プロバイダの API キー

---

## セットアップ / Setup

```bash
git clone https://github.com/<your-name>/LocalAgent.git
cd LocalAgent

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # 使うプロバイダのキーだけ記入 / fill in keys you use
```

ローカルのみで動かす場合は `.env` に `AGENT_OFFLINE=1` を設定すれば、クラウドを
一切使わず ollama だけで動作します（プロバイダのコンテンツポリシーに左右されません）。

---

## 使い方 / Usage

### Web UI（推奨）

```bash
python web_app.py            # 本体コンソール  -> http://127.0.0.1:8770
python web_app2.py           # モデル評価専用  -> http://127.0.0.1:8771
```

1. ブラウザでコンソールを開く
2. モードを `pentest` に
3. 対象（自分が所有・許可を得た環境）を入力
4. 実行モードを選ぶ:
   - **dry-run**（既定）— 計画のみ表示、実行しない
   - **対話承認** — 各操作をボタンで承認
   - **完全自律（exploit まで実行）** — 承認なしで実証まで自動実行

### コマンドライン / CLI

```bash
python agent_loop.py "対象の診断内容を記述"
```

---

## 構成 / Architecture

```
agent_loop.py        本体ループ（goal分解→計画→実行→要約→記憶）
executor.py          実行レイヤー（command/file/code/tool・承認とタイムアウト）
engagement.py        攻撃グラフ（ホスト/サービス/脆弱性/認証情報/発見）
strategist.py        ReAct的な攻撃仮説の生成・採点
providers/           LLMプロバイダのルーティングとアダプタ
tools/               ツール群（registry.py が単一登録点）
engine/              Web検索（DuckDuckGo / Bing / Tavily）
memory/              長期記憶＋ナレッジグラフ
model_bench.py       学科ベンチ（推論/コード/セキュリティ/速度）
ctf_bench.py         実技課題（フラグ式）
web_app.py           本体Webサーバー（ポート8770）
web_app2.py          モデル評価専用サーバー（ポート8771）
web/                 各ページ（コンソール/攻撃グラフ/記憶/ツール/専門家/評価）
test_llm/            分野別テストセット（JSONで追加可能）
web_alert/           脆弱性練習アプリ（ローカル専用・攻略練習用ターゲット）
prompts/             システムプロンプト（英語）
```

### 利用可能なツール / Available tools

`calculator`, `http_get`, `file_summarize`, `git`, `app_detect`, `cve_lookup`,
`metasploit`, `browser`, `vision`, `web_scan`, `web_inspect`, `sqlmap`, `mcp`,
`expert`, `experts_parallel`, `record`, `attack_state`, `exploit_run`, `privesc`,
`lateral`, `strategize`, `report`

---

## モデルの役割割り当て / Model assignment

`/experts` ページで各役割・各ツールに LLM を割り当てられます。`/benchmark`
（または `web_app2.py`）では各モデルを実際にテストし、実測スコアで自動割り当て
できます。テスト問題は `test_llm/` フォルダに分野別 JSON を置いて追加できます。

```json
{
  "domain": "reason",
  "name": "推論",
  "questions": [
    {"q": "問題文", "answer": ["正解1", "正解2"]}
  ]
}
```

---

## ライセンス / License

Apache License 2.0. 詳細は [LICENSE](LICENSE) を参照してください。
See [LICENSE](LICENSE) for details.

---

## 免責事項 / Disclaimer

本ソフトウェアは教育および正当なセキュリティ診断の目的で提供されます。利用者は
適用される全ての法令を遵守する責任を負います。本ツールの使用に起因するいかなる
損害・法的責任についても、作者および貢献者は責任を負いません。

This software is provided for educational and legitimate security-testing purposes.
Users are responsible for complying with all applicable laws. The authors and
contributors are not liable for any damages or legal consequences arising from the
use of this tool.


## 学習アーキテクチャ（使うほど賢くなる）

実行のたびに能力計測と学習が蓄積され、次回の判断が改善される閉ループ:

- **能力ベクトル** (`capabilities.py`): 各モデルの reasoning/planning/tool_usage/security/reflection/speed/refusal_rate をEMAで継続更新。名前推定ではなく実測。
- **AgentBench** (`agentbench.py`): 軌跡をルーブリック採点し planning/tool_usage/reflection を数値化。
- **動的ルーティング** (`router.py`): タスク要求ベクトル×能力ベクトルで最適モデルを毎手選択。
- **Reflection→Replan** (`replan.py`): 失敗を構造化指令に変換し、即時教訓として保存。
- **スキル昇格** (`skill_system.py`): candidate→verified→trusted を成功率で昇格。plannerが信頼スキルを優先再利用。
- **統合記憶** (`memory/long_term.py` recall): lessons/skills/vector/KGを単一IFで横断想起。
- **記憶蒸留** (`consolidation.py`): 反復教訓→スキル昇華、低価値記憶の剪定。

UI: `/capabilities` で能力ベクトルのレーダー表示と記憶整理。
