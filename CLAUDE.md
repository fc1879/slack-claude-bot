# CLAUDE.md

このファイルは Claude Code がこのリポジトリで作業する際の指示書である。

# ⚠️ CRITICAL RULES - MUST READ FIRST

These rules override all other instructions. No exceptions.

This session runs with --dangerously-skip-permissions.
File modifications, command execution, and network access may all be performed directly — **confirmation (Y/N) is not required**.

## Commit & Push Policy

すべての変更は論理的な単位ごとに即座にコミット・プッシュすること。

- 1コミット = 1論理変更（ファイル編集・新規作成・設定変更など）
- コミット直後に必ず `git push origin <branch>` を実行する
- 無関係な変更をまとめて1コミットにしない
- コミットメッセージは変更内容を明確に記述する

## 絶対例外（必ず守ること）

- `tmp/claude_bot_response_*.txt` へのファイル書き出しは、このシステムの根幹となる回答受け渡し処理である。
  全推論・作業が完了した後、そのターンの最終回答のみを Write tool で書き出すこと（下書き・途中経過の書き出し禁止）。

- **DO NOT modify CLAUDE.md under any circumstances without explicit user permission.**
  This file governs the entire operation of this system. Unauthorized edits — including "improvements," additions, or cleanups — are strictly prohibited.
  Always ask the user before making any change to this file.

- **Always obtain user permission before advancing to the next pipeline phase.**
  Agents within the same phase may be called sequentially without interruption:
  - BD/DD phase: Planner → Design Reviewer (no permission needed between them)
  - CD phase: Coder → Code Reviewer (no permission needed between them)
  - Test phase: Test Designer → Test Executor (no permission needed between them)
  - Post-UAT: Retrospective Analyst (always requires explicit permission to start)
  When the current phase completes and the next phase is a different one, stop and ask the user before proceeding.

- Orchestrator が `Agent tool` で生成したサブエージェントは、
  各テンプレートの Outputs / Prohibited Actions に定義されたスコープ内の
  ファイル操作・コマンド実行について即時実行すること。

---

## プロジェクト概要

既存の **Telegram 経由 Claude Code 遠隔操作ボット** を **Slack** へ移行し、
あわせて **チャンネル単位の並列実行** を可能にする。

現行構成（移行元）:

- Telegram Bot API の polling で iPhone からメッセージを受信
- 受信テキストに「出力先ファイルを指定する追加文書」を付与し、`tmux send-keys` で Claude Code セッションへ送信
- Claude Code は最終回答を `tmp/claude_bot_response_{世代番号}.txt` に Write ツールで書き出す
- ボットがファイル出現をポーリング検知し、Telegram へ返信
- 世代番号はプロセス全体で 1 つのグローバル連番。`is_processing` フラグにより同時実行は不可

目標構成（移行先）:

- Slack **Socket Mode**（WebSocket・外向き接続）で受信。公開エンドポイント不要
- **Slack チャンネル = 1 プロジェクト = 1 tmux window** の対応付け
- 状態（世代番号・処理中フラグ・ロック）を**チャンネル単位**に分離し、複数チャンネルの同時実行を実現
- 返信は元メッセージへの**スレッド返信**で紐付け

## 設計上の中核概念

### 1. 応答境界はファイルで判定する（変更しない）

Claude Code の出力ストリームを解析して応答の区切りを判定する方式は採用しない。
「指定パスにファイルが 1 つ生成された = 1 応答が完結した」という原子的な単位を維持する。
この設計は移行後も変更しないこと。

### 2. チャンネル状態オブジェクト

グローバル変数（`_watch_generation`, `is_processing`, `_processing_lock`）は廃止し、
以下の状態オブジェクトへ集約する。

```python
@dataclass
class ChannelState:
    target: str              # tmux の "session:window"
    cwd: str                 # プロジェクト作業ディレクトリ
    tmp: str                 # 応答ファイル出力ディレクトリ
    generation: int = 0
    is_processing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
```

**ロックはチャンネルごとに持つこと。** グローバルロックを共有すると、
一方の処理中に他方の受付が待たされ、並列化の意味が失われる。

### 3. 世代管理

- 世代番号は**チャンネル単位**の連番とする
- `generation += 1` と `is_processing` の確認・更新は必ず `with state.lock` の内側で
  一体の read-modify-write として行う（GIL のアトミック性に依存しない）
- ウォッチャースレッドの失効判定は、自チャンネルの世代とのみ比較する
- `is_processing` の解除は必ず `finally` 節で行う。例外・タイムアウト時に
  チャンネルが永久ロックされることを防ぐ
- 解除時は `state.generation == 自分の gen` を条件とする。
  失効済みスレッドが新しい世代のフラグを誤って落とさないようにするため

### 4. 応答ファイルのパス設計

チャンネル別の世代カウンタにすると、同じ番号のファイルが複数チャンネルで同時に存在し得る。
以下のいずれかで衝突を防ぐこと。

- 推奨: `tmp` ディレクトリをプロジェクトごとに分離する
- 共通 `tmp` を使う場合: `claude_bot_response_{channel_id}_{gen}.txt` とする

### 5. 追加文書（プロンプト付与）

現行の固定パス埋め込みをやめ、チャンネル・世代から動的に生成する。
文面の要件は現行を踏襲すること。

- 出力先パスを唯一の出力チャンネルとして明示
- 全ての推論・作業の**後**に、最終回答のみを Write ツールで書き出させる
- ドラフト・部分書き込みを禁止する
- 確認文（`[確認]` 等）も最終回答として書き出させる
- 日本語で回答させる

## Slack 固有の実装要件

### 認証

Socket Mode には 2 種類のトークンが必要。いずれも `.env` から読み込み、
リポジトリにコミットしないこと。

| トークン | プレフィックス | 用途 |
|---|---|---|
| Bot Token | `xoxb-` | API 呼び出し（投稿・ファイルアップロード） |
| App-Level Token | `xapp-` | Socket Mode 接続（`connections:write` スコープ必須） |

### スレッド返信

受信メッセージの `ts` を世代番号と対応付けて保持し、
返信時に `thread_ts` として指定する。同一チャンネルに複数依頼を投げた際の混線を防ぐ。

### 長文への対応

Slack は 1 メッセージあたり約 3000 文字で切れる。
Claude Code の回答は超えることが多いため、以下の分岐を実装する。

- 閾値以下: `chat_postMessage` でそのまま投稿
- 閾値超過: `files_upload_v2` でスニペットとして投稿（コードブロックの分断を避けられる）
- 分割投稿を選ぶ場合、コードフェンスの途中で切らないこと

### コマンド

- `/reset` — 自チャンネルのみリセット（世代をインクリメントし処理中フラグを解除）
- `/reset all` — 全チャンネルをリセット
- 未登録チャンネルからのメッセージは処理せず、その旨を返信する

## ファイル監視

- 現行のポーリングのままでも動作するが、macOS では `watchdog`（FSEvents）への
  置き換えを検討してよい。レイテンシが下がる
- **書き込み途中のファイルを読まないこと。** ファイルサイズが一定時間変化しないことを
  確認する「落ち着き待ち（settle 判定）」を必ず挟む
- 読み取り後の応答ファイルの扱い（保持 / アーカイブ / 削除）は既存挙動を踏襲する

## 移行手順

トランスポート層の変更と状態設計の変更を**同時に行わないこと。**
以下の順序を守る。

1. **状態のチャンネル分離（Telegram のまま）**
   グローバル変数を `ChannelState` 相当に切り出す。キーは Telegram の chat_id。
   この時点で並列動作を確認する。
2. **Socket Mode の疎通確認**
   オウム返しのみを行う最小の Bolt アプリで WebSocket 接続を確立する。
   tmux 連携は一切含めない。
3. **受信レイヤーの差し替え**
   1 の状態管理はそのままに、入口を Telegram polling から Slack Socket Mode へ置換する。
4. **Slack 固有機能の追加**
   スレッド返信、長文分割、`/reset` のスコープ分離。

各ステップの完了時点で動作確認を行い、次へ進む。

## 実装方針

- 言語 / フレームワーク: Python + `slack_bolt`（`SocketModeHandler`）
- 設定値（チャンネル ID、tmux ターゲット、パス）はコード内に散在させず、
  1 箇所の設定辞書または設定ファイルに集約する
- チャンネル ID をコードにハードコードしない。設定から読む
- 既存の Telegram 実装を破壊的に書き換えず、共通ロジックを抽出したうえで
  トランスポート層を差し替える形を取る

## Agent Team

This project uses a 6-agent development team for feature implementation.
Agent templates are stored in `docs/agents/` and are reusable across all branches.

### Team Composition

| Agent | Phase | Responsibility |
|---|---|---|
| Planner | BD + DD | Define requirements, system design, and implementation plan |
| Design Reviewer | BD/DD Review | Review design for gaps, contradictions, and risks. Approve or send back to Planner |
| Coder | CD | Implement source files strictly within the DD change scope |
| Code Reviewer | CD Review | Review code quality, DD alignment, and security. Approve or send back to Coder |
| Test Designer | UT/IT/ST/UAT Spec | Write test specifications and expected values. Can start after Design Reviewer approves |
| Test Executor | UT/IT/ST/UAT Run | Execute tests per spec and record results. Escalates failures to Orchestrator |
| Retrospective Analyst | Post-UAT | Analyze completed-feature process data and write improvement proposals to `docs/proposals/analyst/` |

### Pipeline

```
Planner → Design Reviewer → (rollback or approve)
                                    ↓ approve
                         Coder ← ← ← ← ← ← ← ← ← ←
                         ↓                          ↑ rollback
                    Code Reviewer → (rollback or approve)
                         ↓ approve
                    Test Executor
                         ↑
                    Test Designer (starts in parallel after Design Reviewer approves)
                         ↓ UAT ✅
                    Retrospective Analyst (spawned by Orchestrator after UAT ✅, read-only)
```

### Orchestrator Rules (this Claude instance)

- Read the relevant `docs/agents/<role>.md` template before spawning each agent.
- Pass the feature number, target files, and prior phase output as context to each agent.
- Never skip a phase. Design Reviewer must approve before Coder starts.
- On rollback: re-spawn the target phase agent with the reviewer's feedback appended to the prompt.
- The shared state between all agents is `docs/featureNNN.md`. Each agent reads it at the start and writes only to its designated sections.

### Template Files

- `docs/agents/planner.md`
- `docs/agents/design-reviewer.md`
- `docs/agents/coder.md`
- `docs/agents/code-reviewer.md`
- `docs/agents/test-designer.md`
- `docs/agents/test-executor.md`
- `docs/agents/retrospective-analyst.md`

### UAT 前チェック（Test Executor 必須）

UAT フェーズ開始前に以下を実施し、結果を `featureNNN.md` の UAT セクションに必ず記録すること。
証跡のない UAT は Pass とみなさない。

```
確認項目:
1. 最新コミット時刻: git log -1 --format="%ci"
2. Bot 起動時刻:    ps aux | grep "python3.*bot.py"
3. 判定: Bot起動時刻 > 最新コミット時刻 → OK / そうでなければ再起動が必要
```

### Feature Completion Checklist

When UAT is marked ✅, the Orchestrator MUST run the following steps in order:

1. Update `docs/feature-list.md` — add one-line summary with `[[]]` links.
2. Spawn `retrospective-analyst` subagent — pass the feature number and the date range of daily reports to read.
3. Record the output proposal path in `docs/featureNNN.md` under `## 関連ドキュメント`.
4. Create or update the daily report `docs/daily-report/daily_report_YYYYMMDD.md` — record the feature number, what was implemented, and key decisions made during the session.
5. Commit & push (proposal file, updated `featureNNN.md`, daily report) in a single commit.
6. Create a Pull Request targeting `main` — all artifacts are now included in the PR.
   (**This is the only point in the feature lifecycle where a PR is created. Do not create PRs at intermediate phases.**)

### featureNNN.md の構成（工程管理テンプレート）

各 feature ファイルは以下の工程を順に記載し、全工程のステータスを管理する。

| 工程 | 略称 | 内容 |
|---|---|---|
| Basic Design | BD | 要件・概要・システム構成の設計 |
| Detail Design | DD | 実装方針・変更スコープ・関数設計の詳細化 |
| Coding | CD | 実装・コードレビュー |
| Unit Test | UT | 関数・モジュール単位の動作確認 |
| Integration Test | IT | コマンド間・モジュール間の連携確認（Slack実機） |
| System Test | ST | システム全体の動作確認（再起動・復元等） |
| User Acceptance Test | UAT | 実運用を想定したユーザー目線での最終確認 |

ステータス表記：`⬜ 未着手` / `🔄 進行中` / `✅ 完了` / `❌ 問題あり`

#### テンプレート

```markdown
## ステータス

| 工程 | ステータス | 完了日 | 備考 |
|---|---|---|---|
| BD | ⬜ | | |
| DD | ⬜ | | |
| CD | ⬜ | | |
| UT | ⬜ | | |
| IT | ⬜ | | |
| ST | ⬜ | | |
| UAT | ⬜ | | |

## BD：基本設計
## DD：詳細設計
## CD：実装スコープ（変更ファイル）
## UT：単体テスト
## IT：結合テスト
## ST：システムテスト
## UAT：ユーザー受入テスト
## PR
  - PR番号・URL・マージ日
## 関連ドキュメント
```

---

## ブランチ戦略

- `main → featureNNN` の2層構成とする（`develop` ブランチは使用しない）
- `featureNNN` ブランチは常に `main` から切る
- PR は当該 feature の全工程（BD/DD/CD/UT/IT/ST/UAT）が完了した時点で1本だけ作成し、`main` へマージする
- 設計フェーズのみ・実装フェーズのみといった中間段階での PR 作成は禁止する

---

## ドキュメント運用ルール

- 機能開発は `featureNNN` ブランチ単位で行う
- ブランチ作成時に `docs/featureNNN.md` を作成し、そのブランチで行った変更・設計判断・バグ修正を全て記載する
- `docs/feature-list.md` にブランチの概要を1行追記し、関連資材を `[[]]` でリンクする
- デイリーレポートは `docs/daily-report/daily_report_YYYYMMDD.md` に記録する
- 残課題・バグ・動作未確認は `docs/issues.md` に随時追記する
- 課題が解決した時点で `docs/issues.md` から該当行を削除し、`docs/issues-resolved.md` に移動する（対応ブランチ・完了日・解決方法を必ず記載する）

---

## 禁止事項

- 秘密情報（トークン、絶対パスを含む個人環境情報）のコミット
- グローバルな可変状態の再導入
- 応答境界をストリーム解析で判定する方式への変更
- `--dangerously-skip-permissions` の前提を崩す変更を、確認なく行うこと

## 作業時の確認事項

仕様が曖昧な箇所は推測で実装せず、`[確認]` を付けて質問すること。
特に以下は既存実装の詳細確認を要する。

- 応答ファイルの後始末の既存挙動
- タイムアウト値と、タイムアウト時のユーザーへの通知内容
- 世代管理の細部（現行の「厳密な仕様」部分）
