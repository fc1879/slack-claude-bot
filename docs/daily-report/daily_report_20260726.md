# Daily Report — 2026-07-26

## 作業概要

| 項目 | 内容 |
|---|---|
| 対象機能 | feature000 — Core Bot Implementation: Slack tmux Relay with Per-Channel State |
| フェーズ | UT（完了）/ IT・ST・UAT（環境待ち） |
| 担当 | Orchestrator + Test Executor (subagent) |

---

## 実施内容

### UT フェーズ完了（T-01 〜 T-41 / 全 41 件 Pass）

前日（2026-07-25）に実装が完了した `src/` モジュール群に対し、コードインスペクション方式で単体テストを実施した。

| モジュール | テスト件数 | 結果 |
|---|---|---|
| `src/config.py` (`_load_channel_map`) | T-01〜T-05 (5件) | ✅ 全 Pass |
| `src/channel_state.py` (`init_channel_states`) | T-06〜T-07 (2件) | ✅ 全 Pass |
| `src/tmux_handler.py` | T-08〜T-17 (10件) | ✅ 全 Pass |
| `src/file_handler.py` (`send_long_text`) | T-18〜T-20 (3件) | ✅ 全 Pass |
| `src/bot.py` (`_build_prompt`) | T-21 (1件) | ✅ Pass |
| `src/bot.py` (`handle_message`) | T-22〜T-32 (11件) | ✅ 全 Pass |
| `src/bot.py` (`handle_reset`) | T-33〜T-36 (4件) | ✅ 全 Pass |
| `src/bot.py` (`_watch_for_response`) | T-37〜T-41 (5件) | ✅ 全 Pass |

### IT・ST・UAT は実環境待ちで保留

T-42〜T-68（IT×13件、ST×8件、UAT×6件）は Slack ワークスペース・tmux セッション・Claude Code の実環境が必要なため、環境準備完了後に実施予定。

### Slack App セットアップのスコープ確認

IT/ST/UAT 実施に必要な Slack App の Bot Token Scopes を確認・決定した。

---

## 主要な判断・決定事項

### Slack Bot Token Scopes（5つに絞り込み）

実装（`src/bot.py`）を精査し、必要最小限のスコープを特定した。

| スコープ | 根拠 |
|---|---|
| `app_mentions:read` | `@app.event("app_mention")` でチャンネルメンションを受信 |
| `im:history` | `@app.event("message")` で DM を受信（`channel_type == "im"` のみ通過） |
| `chat:write` | `chat_postMessage`（返信・拒否・タイムアウト通知）。ボットはチャンネルメンバーとして追加するため `chat:write.public` は不要 |
| `files:write` | `files_upload_v2`（3000文字超の長文をスニペット投稿） |
| `commands` | `/reset` スラッシュコマンド（Slash Commands 追加で自動付与） |

`channels:history` / `groups:history` は不要（チャンネルの `message` イベントは step-0 ガードで全て無視するため）。

---

## 残タスク・次アクション

| タスク | 担当 | 状態 |
|---|---|---|
| Slack App 作成（Bot Token / App-Level Token 取得） | ユーザー | 進行中 |
| `.env` ファイル作成と `CHANNEL_MAP_JSON` 設定 | ユーザー | 未着手 |
| tmux セッション + Claude Code 起動確認 | ユーザー | 未着手 |
| IT（T-42〜T-54）実施 | Test Executor | 環境待ち |
| ST（T-55〜T-62）実施 | Test Executor | 環境待ち |
| UAT（T-63〜T-68）実施 | Test Executor | 環境待ち |
| UAT ✅ 後の Feature Completion Checklist 実行 | Orchestrator | 未着手 |

---

## 特記事項

なし

---

## Memo

### Slack Bot Token Scopes 選定結果

今回選定したスコープ（5つ）：

| スコープ | 根拠 |
|---|---|
| `app_mentions:read` | チャンネルの `@bot` メンションを受信（`app_mention` イベント） |
| `im:history` | DM の `message` イベントを受信（`channel_type == "im"` のみ通過） |
| `chat:write` | `chat_postMessage`（返信・拒否・タイムアウト通知）。ボットはチャンネルにメンバーとして追加するため `chat:write.public` は不要 |
| `files:write` | `files_upload_v2`（3000文字超の長文をスニペット投稿） |
| `commands` | `/reset` スラッシュコマンド（Slash Commands 追加で自動付与） |

---

今後追加の可能性があるスコープ：

**優先度：高**（詰まる可能性があり、足す判断になりやすい）

| スコープ | 追加が必要になるケース |
|---|---|
| `im:read` | DM 疎通で `missing_scope` が出たときの補完。`im:history` だけで本文が読めないケースがあり、DM 運用の第一関門になりやすい |

**優先度：中**（運用の質を上げるが無くても動く）

| スコープ | 追加が必要になるケース |
|---|---|
| `users:read` | 通知文やログで user ID ではなくユーザー名を扱いたい場合 |
| `reactions:write` | 処理中🔄・完了✅など、状態を絵文字で可視化したい場合 |
| `channels:history` | メンションなしのスレッド継続返信も拾いたくなった場合（メンション運用を崩す拡張なので現時点では保留でよい） |

**優先度：低**（今回の設計では基本不要）

| スコープ | 追加が必要になるケース |
|---|---|
| `files:read` | 自分が上げたスニペットを読み返す必要が出た場合のみ |
| `chat:write.public` | 未参加チャンネルへ投稿する運用に変えた場合のみ（現状はメンバー追加前提なので不要） |
| `channels:read` / `groups:read` | チャンネル名・ID を動的に取得したい場合のみ（設定辞書に ID を持つ CLAUDE.md の方針なら不要） |
