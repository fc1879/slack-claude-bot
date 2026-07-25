# feature000 — Core Bot Implementation: Slack tmux Relay with Per-Channel State

## ステータス

| 工程 | ステータス | 完了日 | 備考 |
|---|---|---|---|
| BD | ✅ | 2026-07-25 | |
| DD | ✅ | 2026-07-25 | Design Review (Final) approved |
| CD | ✅ | 2026-07-25 | |
| UT | ⬜ | | |
| IT | ⬜ | | |
| ST | ⬜ | | |
| UAT | ⬜ | | |

---

## Overview

現在の `app.py` は Slack Socket Mode の動作確認用プレースホルダーであり、Claude API を直接呼び出している。tmux 連携、チャンネル認可、スレッド返信、応答ファイルリレー等の本来機能はいずれも未実装である。

本 feature では `app.py` を廃止し、`src/` モジュール構成に全面移行することで CLAUDE.md に定義された目標構成を実現する。

**解決する問題:**
- tmux 連携がなく、ユーザーのメッセージが Claude Code セッションへ届かない
- チャンネル認可がなく、任意チャンネルからの要求を受け付けてしまう
- グローバル状態のため並列実行不可
- スレッド返信がなく、複数依頼の混線を防げない
- 長文応答が Slack の文字数制限で切れる

---

## BD：基本設計

### 要件

1. **tmux 連携**: Slack メッセージ受信時、対応する tmux window に Claude Code へのプロンプトを送信する
2. **チャンネル = tmux window 対応付け**: 設定辞書 `CHANNEL_MAP` でチャンネル ID → tmux ターゲット・パスをマッピングする
3. **チャンネル単位の並列実行**: `ChannelState` を各チャンネルに独立して持ち、チャンネル間で処理がブロックしない
4. **応答ファイルリレー**: Claude Code が `tmp/{channel_id}_{gen}.txt` に書き出したファイルをポーリング検知し、Slack スレッドへ返信する
5. **スレッド返信**: 受信メッセージの `ts` を `thread_ts` として使用し、依頼と応答を同スレッドに紐付ける
6. **長文対応**: ≤3000 文字は `chat_postMessage`、超過は `files_upload_v2` スニペットとして投稿する
7. **`/reset` コマンド**: 自チャンネルのみ世代をインクリメントし `is_processing` を解除する
8. **`/reset all` コマンド**: 全チャンネルを同様にリセットする
9. **チャンネル認可**: 未登録チャンネルからの入力は処理せずエラーメッセージを返信する

### 影響コンポーネント

| コンポーネント | 変更種別 | 理由 |
|---|---|---|
| `app.py` | 廃止（bot.py エントリポイントへ移行） | プレースホルダーであり、設計と乖離している |
| `src/config.py` | 新規作成 | チャンネルマッピングと定数の集約 |
| `src/channel_state.py` | 新規作成 | チャンネル単位状態管理 |
| `src/tmux_handler.py` | 新規作成 | tmux 操作の抽象化 |
| `src/file_handler.py` | 新規作成 | Slack 長文送信の抽象化 |
| `src/bot.py` | 新規作成 | メインハンドラー・エントリポイント |
| `.env.example` | 更新 | `ANTHROPIC_API_KEY` と `CLAUDE_MODEL` を削除し、`CHANNEL_MAP_JSON` を追加 |
| `requirements.txt` | 更新 | `watchdog` を追加（任意）、`anthropic` を削除 |

### 応答境界の原則

応答の区切りはストリーム解析ではなく「指定パスにファイルが生成された」という原子的な事実で判定する。この原則は本 feature でも変更しない。

### Slack API 冪等性

`chat_postMessage` および `files_upload_v2` は冪等でない。同一リクエストを2回送信すると2つのメッセージが生成される。本実装では「送信前に接続確認 → 1回送信 → 送信後リトライなし」の方針を厳守する。

---

## DD：詳細設計

### 変更スコープ

| ファイル | 変更種別 | 概要 |
|---|---|---|
| `src/config.py` | 新規作成 | チャンネル→tmux マッピング、定数定義 |
| `src/channel_state.py` | 新規作成 | ChannelState dataclass、初期化関数 |
| `src/tmux_handler.py` | 新規作成 | tmux サブプロセス操作 |
| `src/file_handler.py` | 新規作成 | Slack 長文送信 |
| `src/bot.py` | 新規作成 | Slack イベントハンドラー、エントリポイント |
| `src/__init__.py` | 新規作成 | 空ファイル（パッケージ宣言） |
| `.env.example` | 更新 | 環境変数テンプレート |
| `requirements.txt` | 更新 | 依存パッケージ更新 |

---

### `src/config.py`

#### `ChannelConfig` (dataclass)

```python
@dataclass
class ChannelConfig:
    target: str   # tmux "session:window" 形式（例: "claude_session:project-a"）
    cwd: str      # Claude Code の作業ディレクトリ（絶対パス）
    tmp: str      # 応答ファイル出力ディレクトリ（絶対パス）
```

#### 定数

| 定数名 | 型 | 値 | 説明 |
|---|---|---|---|
| `TMUX_SESSION` | `str` | `os.environ.get("TMUX_SESSION", "claude_session")` | tmux セッション名 |
| `RESPONSE_TIMEOUT` | `int` | `int(os.environ.get("RESPONSE_TIMEOUT", "120"))` | 応答待ちタイムアウト秒数 |
| `POLL_INTERVAL` | `float` | `2.0` | ファイルポーリング間隔（秒） |
| `SETTLE_DURATION` | `float` | `1.0` | settle判定の安定確認待ち時間（秒） |
| `MAX_MESSAGE_LENGTH` | `int` | `3000` | Slack 投稿の文字数閾値 |
| `CHANNEL_MAP` | `dict[str, ChannelConfig]` | `_load_channel_map()` で構築 | チャンネル ID → ChannelConfig |

#### `_load_channel_map() -> dict[str, ChannelConfig]`

- `CHANNEL_MAP_JSON` 環境変数から JSON 文字列を読み込む
- JSON スキーマ（1チャンネルの例）: `{"C01234ABCDE": {"target": "claude_session:project-a", "cwd": "/path/to/project", "tmp": "/path/to/tmp"}}`
- 複数チャンネルはトップレベルオブジェクトにキーを追加するだけでよい: `{"C01234ABCDE": {...}, "C09999ZZZZZ": {...}}`
- パース失敗時は `ValueError` を raise し、起動時に即座にクラッシュさせる（サイレント起動禁止）
- **`cwd` および `tmp` ディレクトリは起動前に存在していなければならない。** ボットはこれらを自動作成しない。存在しない場合、応答ファイル書き込み時に `FileNotFoundError` が発生する（起動時チェックは行わない）。

---

### `src/channel_state.py`

#### `ChannelState` (dataclass)

```python
@dataclass
class ChannelState:
    target: str
    cwd: str
    tmp: str
    generation: int = 0
    is_processing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
```

**不変条件:**
- `generation` と `is_processing` の読み取り・更新は必ず `with state.lock:` の内側で行う
- `is_processing = False` の解除は必ず `finally` 節で行う
- 解除時は `state.generation == own_gen` を条件とする（失効スレッドが新世代フラグを誤って解除しないため）

#### `init_channel_states(channel_map: dict[str, ChannelConfig]) -> dict[str, ChannelState]`

- `channel_map` の各エントリに対して `ChannelState(target=..., cwd=..., tmp=...)` を生成して返す
- `bot.py` の起動時に1回だけ呼び出す

---

### `src/tmux_handler.py`

全関数は `subprocess.run` でコマンドを実行し、成功時は `True` または文字列を返す。失敗時は `RuntimeError` を raise する（`check=True`）。シェルインジェクション防止のため、引数は常にリスト形式で渡す（`shell=False`）。

#### `session_exists(session: str) -> bool`

- `tmux has-session -t {session}` を実行する
- 戻り値: セッションが存在すれば `True`、しなければ `False`
- 例外は raise しない（存在しない場合は非ゼロ終了コードを `check=False` で捕捉）

#### `window_exists(session: str, window: str) -> bool`

- `tmux list-windows -t {session} -F "#W"` を実行し、出力行に `window` が含まれているか確認する
- 戻り値: `bool`

#### `send_input(session: str, text: str, window: str) -> None`

- `subprocess.run` をリスト形式（`shell=False`）で呼び出す。`text` はリスト要素としてそのまま渡す（クォート処理不要）:
  ```python
  subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window}", text, "Enter"], check=True)
  ```
- Enter 送信後 0.3 秒 `time.sleep` する（tmux バッファへの確実な書き込みのため）
- **`shlex.quote` は使用しない。** `subprocess.run` リスト形式ではシェルが引数を展開しないため、クォート処理は不要かつ有害（適用するとリテラルなシングルクォート文字が tmux 経由で Claude Code に送信され、プロンプトが破損する）。

  **実装注意**: `tmux send-keys` の第3引数（keys）はリスト要素として渡す。改行・特殊文字を含む長いテキストは `tmux send-keys` の代わりに `xdotool` や別手法を検討する必要があるが、本 feature では既存 Telegram Bot と同様に `send-keys` を使用する。

#### `create_window(session: str, window: str, cwd: str) -> None`

- `tmux new-window -t {session} -n {window} -c {cwd}` を実行する
- その後、Claude Code を起動するコマンドを `send_input` 経由で送信する: `claude --dangerously-skip-permissions`
- ウィンドウ生成後 1.0 秒 `time.sleep` する（Claude Code 起動待ち）

#### `ensure_window(session: str, window: str, cwd: str) -> None`

- `window_exists` で確認し、存在しなければ `create_window` を呼ぶ
- `bot.py` の初期化時およびメッセージ受信時に呼ぶ

---

### `src/file_handler.py`

#### `send_long_text(client, channel: str, text: str, thread_ts: str) -> None`

- **冪等性**: `chat_postMessage` および `files_upload_v2` は冪等でない。本関数は送信を1回だけ行い、送信後のリトライは行わない。
- `len(text) <= MAX_MESSAGE_LENGTH` の場合:
  - `client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)` を1回呼ぶ
- `len(text) > MAX_MESSAGE_LENGTH` の場合:
  - `client.files_upload_v2(channel=channel, content=text, filename="response.md", filetype="markdown", thread_ts=thread_ts)` を1回呼ぶ
  - `filetype="post"` ではなく `"markdown"` を使用する（コードフェンスの視認性のため）
- いずれも Slack SDK が `SlackApiError` を raise した場合は呼び出し元に伝播させる（ここでは再送しない）

---

### `src/bot.py`

#### グローバル変数

| 変数名 | 型 | 初期値 | 説明 |
|---|---|---|---|
| `_channel_states` | `dict[str, ChannelState]` | `init_channel_states(CHANNEL_MAP)` | チャンネル ID → ChannelState |

#### `_build_prompt(message: str, response_file: str) -> str`

- 以下の追加文書を `message` に付加して返す（文字列結合）:

```
{message}

---
[TOP PRIORITY] 以下の指示に従うこと:
1. 上記の要求への全ての推論・作業が完了した後、最終回答のみを以下のパスに Write ツールで書き出すこと。
2. 途中経過・下書き・部分的な内容を書き出してはならない。
3. `[確認]` のような確認文も最終回答として書き出すこと。
4. 回答は日本語で記述すること。
5. 以下のパス以外へ書き出してはならない。

出力先パス（Write ツールで書き出す先）:
{response_file}
```

**設計意図**: Claude Code が Write ツールで `{response_file}` に直接書き出す。ボットはこのパスをポーリングし、ファイルが出現したら読み取る。mv ステップは不要。既存の Telegram Bot と同等のシンプルな設計を維持する。

#### `_watch_for_response(response_file: str, channel_id: str, thread_ts: str, own_gen: int, client) -> None`

- バックグラウンドスレッドとして実行される（`threading.Thread(target=..., daemon=True)`）
- 関数先頭で以下を取得する:
  - `state = _channel_states[channel_id]`（`_channel_states` 辞書自体は初期化後に変更されないため、lock なしでスレッドから参照して安全）
  - `start_time = time.time()`
- **検出対象は `response_file` のみ。** Claude Code が Write ツールで直接書き出すファイルをポーリングする。mv ステップなし。書き込み途中のファイルを読まないよう、ファイル出現後にサイズ安定を確認する settle 判定を挟む。

**ポーリングループの動作（`POLL_INTERVAL` 秒間隔）:**

1. 世代失効チェック: `with state.lock:` 内で `state.generation != own_gen` なら即座にリターンする（失効スレッド）
2. タイムアウト判定: `time.time() - start_time >= RESPONSE_TIMEOUT` なら タイムアウト処理へ移行する
3. `os.path.exists(response_file)` を確認する
4. 存在する場合（settle 判定ブランチ）:
   a. `size1 = os.path.getsize(response_file)` を記録する
   b. `time.sleep(SETTLE_DURATION)` で安定待ちする
   c. 世代失効チェック: `with state.lock:` 内で `state.generation != own_gen` なら即座にリターンする（settle 待ち中に /reset が来た場合に対応）
   d. タイムアウト判定: `time.time() - start_time >= RESPONSE_TIMEOUT` なら タイムアウト処理へ移行する（settle 待ち中にタイムアウトを超過した場合に対応）
   e. `os.path.exists(response_file)` を再確認する
      - 存在する場合:
        - `size2 = os.path.getsize(response_file)` を取得する
        - `size1 == size2`（サイズ変化なし = 書き込み完了）なら:
          - `response_file` からファイル内容を読み取る
          - `response_file` を削除する（`os.remove`）
          - `file_handler.send_long_text(client, channel_id, content, thread_ts)` を1回呼ぶ
          - ループを抜けてリターンする
        - `size1 != size2`（まだ書き込み中）なら: ポーリングループ先頭へ戻る（`time.sleep(POLL_INTERVAL)` を挟んで次ポーリングへ）
      - 存在しない場合（settle 待ち中にファイルが削除された）: ポーリングループ先頭へ戻る
5. ファイルが存在しない場合: `time.sleep(POLL_INTERVAL)` してループ先頭へ戻る

- タイムアウト（`RESPONSE_TIMEOUT` 秒経過）した場合:
  - スレッド返信: `"タイムアウトしました。Claude Code が応答ファイルを生成しませんでした。/reset で再試行してください。"`
- `is_processing` 解除のコード（`finally` 節内、タイムアウト・正常完了・失効終了いずれのパスでも実行される）:
  ```python
  with state.lock:
      if state.generation == own_gen:
          state.is_processing = False
  ```

#### `handle_message(event, client, logger) -> None`

- トリガー: `@app.event("app_mention")` および `@app.event("message")` （DM）
- 処理手順:
  0. **二重処理ガード**: `event.get("type") == "message"` かつ `event.get("channel_type") != "im"` の場合、即座に `return` する（チャンネルメッセージは `app_mention` イベントでも発火するため、`message` イベント側では無視する。DM は `channel_type == "im"` のため通過させる）。
  1. `channel_id = event["channel"]`、`thread_ts = event.get("thread_ts") or event["ts"]` を取得する
  2. `channel_id not in _channel_states` なら `client.chat_postMessage(channel=channel_id, text="このチャンネルは未登録です。管理者に連絡してください。", thread_ts=thread_ts)` を送信して `return`
  3. メッセージテキストを取得し、`<@BOTID>` メンション部分を `re.sub(r"<@[A-Z0-9]+>", "", text).strip()` で除去する
  4. テキストが空なら `"メッセージが空です。"` を返信して `return`
  5. `state = _channel_states[channel_id]` を取得する
  6. `with state.lock:` 内で:
     - `state.is_processing` が `True` なら `"処理中です。完了をお待ちください。/reset で中断できます。"` を返信して `return`
     - `state.generation += 1`
     - `state.is_processing = True`
     - `own_gen = state.generation` を記録する
  7. パスを構築する:
     - `response_file = os.path.join(state.tmp, f"claude_bot_response_{channel_id}_{own_gen}.txt")`
  8. 前世代の残留ファイルを除去する（`{channel_id}` と `{own_gen - 1}` で以下を構築してチェック）:
     - `prev_file = os.path.join(state.tmp, f"claude_bot_response_{channel_id}_{own_gen - 1}.txt")`
     - 存在する場合は `os.remove` で削除する
  9. `ensure_window(TMUX_SESSION, state.target.split(":")[-1], state.cwd)` を呼ぶ
  10. `full_prompt = _build_prompt(clean_text, response_file)` を呼ぶ
  11. `tmux_handler.send_input(TMUX_SESSION, full_prompt, window=state.target.split(":")[-1])` を呼ぶ
  12. `client.chat_postMessage(channel=channel_id, text=f"送信しました... [チャンネル: {channel_id}]", thread_ts=thread_ts)` を送信する
  13. `threading.Thread(target=_watch_for_response, args=(response_file, channel_id, thread_ts, own_gen, client), daemon=True).start()` でウォッチャーを起動する
  - **例外処理**: ステップ 6 の `with state.lock:` ブロックが閉じた直後に `watcher_started = False` フラグを宣言し、ステップ 7〜13 全体を `try:` ブロックで囲む。`Thread.start()` 成功直後に `watcher_started = True` をセットする。`finally` 節では `not watcher_started` の場合のみ `is_processing` を解除する。構造を以下に示す:
    ```python
    with state.lock:
        # check is_processing → reject or set is_processing = True, own_gen = generation
        ...

    watcher_started = False
    try:
        # steps 7–12: build response_file path, cleanup prev gen file, build prompt, send to tmux, post "送信しました" reply
        ...
        threading.Thread(target=_watch_for_response, args=(response_file, channel_id, thread_ts, own_gen, client), daemon=True).start()
        watcher_started = True  # ← set AFTER successful Thread.start()
    finally:
        if not watcher_started:
            with state.lock:
                if state.generation == own_gen:
                    state.is_processing = False
    # Note: when watcher_started=True, _watch_for_response's own finally block is responsible
    # for clearing is_processing
    ```
    `try:` の開始位置は `with state.lock:` ブロックの **直後**（ロックを保持しない状態）である。ウォッチャースレッドが正常起動した場合、`is_processing` の解除責任は `_watch_for_response` の `finally` 節に委譲される。`handle_message` 側では解除しない。

#### `handle_reset(ack, command, client, logger) -> None`

- トリガー: `/reset` Slack スラッシュコマンド
- `ack()` を最初に呼ぶ（Slack の3秒タイムアウト対応）
- `channel_id = command["channel_id"]` を取得する
- `channel_id not in _channel_states` なら `"このチャンネルは未登録です。"` を返信して `return`
- `state = _channel_states[channel_id]` で状態オブジェクトを取得する
- `with state.lock:` 内で `state.generation += 1`、`state.is_processing = False` を行う
- `client.chat_postMessage(channel=channel_id, text=f"チャンネルをリセットしました。(generation={state.generation})")` を返信する

#### `handle_reset_all(ack, command, client, logger) -> None`

- トリガー: `/reset` コマンドで `command["text"].strip() == "all"` の場合に `handle_reset` から分岐（または `/reset_all` として登録）
- 実装方針: `handle_reset` 内で `command.get("text", "").strip() == "all"` を判定し、`all` の場合は全チャンネルをリセットする
  - 全 `_channel_states` をループし、各 `state` に対して `with state.lock:` 内で `generation += 1`、`is_processing = False` を行う
  - 完了後 `"全チャンネルをリセットしました。"` を返信する
- **注**: `/reset` と `/reset all` は同一 Slack コマンド (`/reset`) であり、`command["text"]` でサブコマンドを区別する

#### `main() -> None`

- `load_dotenv()` を呼ぶ
- `App(token=os.environ["SLACK_BOT_TOKEN"])` でアプリを初期化する
- イベントハンドラーを登録する: `app.event("app_mention")(handle_message)`、`app.event("message")(handle_message)`、`app.command("/reset")(handle_reset)`。`handle_message` はチャンネルメンション（`app_mention`）と DM（`message` + `channel_type == "im"`）の両方を処理するが、`message` イベントがチャンネルで発火した場合はステップ 0 のガードで即座に `return` されるため二重処理は発生しない。
- `_channel_states` をモジュールレベルで `init_channel_states(CHANNEL_MAP)` で初期化する
- 全チャンネルの tmux window を `ensure_window` で事前確認する
- `SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()` を呼ぶ

---

### `.env.example` の更新内容

現行の `.env.example` から `ANTHROPIC_API_KEY` と `CLAUDE_MODEL` を削除し、以下に置き換える:

```
SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
SLACK_APP_TOKEN=xapp-your-slack-app-token
TMUX_SESSION=claude_session
RESPONSE_TIMEOUT=120

# CHANNEL_MAP_JSON: チャンネルID → tmuxターゲット・パスのマッピング（JSON形式）
# チャンネルIDはSlackの「チャンネル詳細」から取得できる（例: C01234ABCDE）
# target: tmux の "session:window" 形式
# cwd:    Claude Code の作業ディレクトリ（絶対パス、起動前に存在していること）
# tmp:    応答ファイル出力ディレクトリ（絶対パス、起動前に存在していること）
#
# 1チャンネルの例:
CHANNEL_MAP_JSON={"C01234ABCDE": {"target": "claude_session:project-a", "cwd": "/absolute/path/to/project", "tmp": "/absolute/path/to/tmp"}}
#
# 複数チャンネルの例:
# CHANNEL_MAP_JSON={"C01234ABCDE": {"target": "claude_session:project-a", "cwd": "/path/to/project-a", "tmp": "/path/to/tmp-a"}, "C09999ZZZZZ": {"target": "claude_session:project-b", "cwd": "/path/to/project-b", "tmp": "/path/to/tmp-b"}}
```

---

### `requirements.txt` の更新内容

`anthropic` パッケージを削除する（tmux 経由で Claude Code を操作するため、Anthropic SDK は不要）。

```
slack-bolt>=1.21.0
python-dotenv>=1.0.0
```

---

## CD：実装スコープ（変更ファイル）

| ファイル | 変更種別 | 説明 |
|---|---|---|
| `src/__init__.py` | 新規作成 | パッケージ宣言（空ファイル） |
| `src/config.py` | 新規作成 | `ChannelConfig` dataclass、定数、`_load_channel_map()` |
| `src/channel_state.py` | 新規作成 | `ChannelState` dataclass、`init_channel_states()` |
| `src/tmux_handler.py` | 新規作成 | `session_exists`、`window_exists`、`send_input`、`create_window`、`ensure_window` |
| `src/file_handler.py` | 新規作成 | `send_long_text` |
| `src/bot.py` | 新規作成 | `_build_prompt`、`_watch_for_response`、`handle_message`、`handle_reset`、`main` |
| `.env.example` | 更新 | `ANTHROPIC_API_KEY`/`CLAUDE_MODEL` を削除、`CHANNEL_MAP_JSON` 等を追加 |
| `requirements.txt` | 更新 | `anthropic` を削除 |

**注**: `app.py` は削除せず、残置する。`src/bot.py` が新たなエントリポイントとなる（`python src/bot.py` で起動）。将来的に `app.py` を削除するかどうかは別 feature で判断する。

### Change Summary

**`src/__init__.py`**
空ファイル。`src` をパッケージとして認識させるための宣言。

**`src/config.py` — `ChannelConfig`、定数、`_load_channel_map()`**
- `ChannelConfig(target, cwd, tmp)` dataclass を定義。
- `TMUX_SESSION`、`RESPONSE_TIMEOUT`、`POLL_INTERVAL`、`SETTLE_DURATION`、`MAX_MESSAGE_LENGTH` 定数を定義（全て環境変数または固定値から取得）。
- `_load_channel_map()`: `CHANNEL_MAP_JSON` 環境変数を JSON パースし `dict[str, ChannelConfig]` を返す。環境変数未設定・JSON パースエラー・キー不足のいずれでも `ValueError` を raise して起動クラッシュさせる（サイレント起動禁止）。
- モジュールロード時に `CHANNEL_MAP = _load_channel_map()` を呼び出し、起動時に即座に検証する。

**`src/channel_state.py` — `ChannelState`、`init_channel_states()`**
- `ChannelState(target, cwd, tmp, generation=0, is_processing=False, lock=Lock())` dataclass を定義。
- `init_channel_states(channel_map)`: `CHANNEL_MAP` の各エントリに対応する `ChannelState` を生成して返す。`bot.py` 起動時に1回だけ呼ぶ。

**`src/tmux_handler.py` — tmux サブプロセス操作**
- `session_exists(session)`: `tmux has-session` で存在確認。`check=False` でゼロ/非ゼロを bool に変換。
- `window_exists(session, window)`: `tmux list-windows -F "#W"` の出力行に window 名が含まれるか確認。
- `send_input(session, text, window)`: `subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window}", text, "Enter"], check=True)` をリスト形式で呼ぶ。`shlex.quote` は使用しない（shell が関与しないため不要かつ有害）。送信後 0.3 秒 sleep。
- `create_window(session, window, cwd)`: `tmux new-window` で window 作成後、1.0 秒 sleep してから `claude --dangerously-skip-permissions` を `send_input` で送信。
- `ensure_window(session, window, cwd)`: `window_exists` で確認し、存在しなければ `create_window` を呼ぶ。

**`src/file_handler.py` — `send_long_text()`**
- `len(text) <= MAX_MESSAGE_LENGTH` なら `chat_postMessage`、超過なら `files_upload_v2(filetype="markdown")` で1回送信。送信後リトライなし。`SlackApiError` は呼び出し元へ伝播。

**`src/bot.py` — メインハンドラー・エントリポイント**
- `_build_prompt(message, response_file)`: メッセージに `[TOP PRIORITY]` 指示を付加。日本語回答、最終回答のみ、指定パスへの Write のみを強制。
- `_watch_for_response(response_file, channel_id, thread_ts, own_gen, client)`:
  - バックグラウンドスレッドとして daemon=True で動作。
  - ポーリングループ先頭で世代失効チェック（`state.generation != own_gen` → return）とタイムアウト判定。
  - ファイル検出時: `size1` 記録 → `SETTLE_DURATION` sleep → 世代/タイムアウト再チェック → `size2` 取得 → `size1 == size2` なら読み取り・削除・送信してリターン、不一致ならループ先頭へ戻る。
  - `finally` 節: `state.generation == own_gen` のときのみ `is_processing = False` で解除。
- `handle_message(event, client, logger)`:
  - Step 0: `type == "message"` かつ `channel_type != "im"` ならガード return（app_mention との二重処理防止）。
  - Step 2: チャンネル認可チェックを先頭で実施。
  - Step 6: `with state.lock:` 内で `is_processing` チェック → `generation += 1` → `is_processing = True` → `own_gen` 記録。
  - `watcher_started = False` を宣言後、`try` ブロックでステップ 7〜13 を囲む。`Thread.start()` 成功直後に `watcher_started = True`。`finally` では `not watcher_started` のときのみ `is_processing` を解除（ウォッチャー起動後はウォッチャーの `finally` に委譲）。
- `handle_reset(ack, command, client, logger)`:
  - `ack()` を最初に呼ぶ。`command["text"] == "all"` で全チャンネルリセット、そうでなければ自チャンネルのみリセット。各リセットは `with state.lock:` 内で `generation += 1` + `is_processing = False` をアトミックに実行。
- `main()`: `load_dotenv()` → `App` 初期化 → イベント登録 → 全チャンネルの `ensure_window` → `SocketModeHandler.start()`。

---

## UT：単体テスト

（Test Designer フェーズで記載）

## IT：結合テスト

（Test Designer フェーズで記載）

## ST：システムテスト

（Test Designer フェーズで記載）

## UAT：ユーザー受入テスト

（Test Executor フェーズで記載）

## PR

（実装完了後に記載）

## 関連ドキュメント

（Retrospective Analyst フェーズで記載）

---

## Design Review (Addendum — CHANNEL_MAP_JSON concern)

Status: ✅ Additions approved

### Assessment

#### Addition 1 — Multi-channel JSON format example in `_load_channel_map()`

The single-channel example was already present. The multi-channel example (`{"C01234ABCDE": {...}, "C09999ZZZZZ": {...}}`) and the accompanying note ("トップレベルオブジェクトにキーを追加するだけでよい") are purely illustrative. They add no new implementation requirements, no new invariants, and no new code paths. The multi-channel structure is already implied by the `dict[str, ChannelConfig]` return type — the example simply makes it unambiguous to the operator configuring the environment variable. The addition is appropriate and helpful.

#### Addition 2 — "Directories must pre-exist" note and `.env.example` inline comments

The note in `_load_channel_map()` (line 115) and the matching inline comments in `.env.example` (lines 333–334) make an implicit behavior explicit: the bot does not auto-create `cwd` or `tmp` directories.

This is the correct design choice for two reasons:

1. `cwd` is a pre-existing Claude Code project directory. Auto-creating it at a typo'd path would silently mask misconfiguration. Letting `tmux new-window -c {cwd}` fail with a non-existent path is the right failure mode.
2. `tmp` not existing will cause `FileNotFoundError` when Claude Code attempts to write the response file. This is a visible, diagnosable failure — not a silent one.

The note also documents that no startup check is performed. This tradeoff (late detection on first message vs. early detection at boot) is acceptable for this system and is now documented rather than assumed. The note does not conflict with any existing DD specification or CLAUDE.md principle.

Both additions are internally consistent: the `_load_channel_map()` note and the `.env.example` comments say the same thing in the same terms.

---

## Design Review

Status: ❌ Rejected

Issues:

### Issue A — `send_input` の `shlex.quote` 指示が矛盾しており、実装するとプロンプトが破損する

DD `src/tmux_handler.py` の `send_input` に以下の矛盾がある:

> `text` は `shlex.quote` でクォートしてから渡す（シェルインジェクション防止）
> 注: `subprocess.run` の引数リストに渡す場合、`shlex.quote` は不要。

`subprocess.run` をリスト形式（`shell=False`）で呼ぶ場合、シェルが引数を展開しないため `shlex.quote` は不要であるというのは正しい。しかし「tmux の `-t` フラグのターゲット指定に用いること」という注記は意味をなさない（`-t session:window` はユーザー入力ではない）。

**根本問題**: Coder がこの指示どおりに `shlex.quote(text)` を適用してリスト要素として渡すと、tmux は literal なシングルクォート文字（`'...'`）を Claude Code セッションへ送信する。結果として Claude Code が受け取るプロンプトが `'ユーザーの入力'` のように壊れ、動作しない。

修正依頼: `send_input` の実装指示を以下のいずれか一方に統一すること。
- `subprocess.run` リスト形式 + `shlex.quote` なし（推奨）
- f-string による文字列結合 + `shell=True` + `shlex.quote`（非推奨。シェルインジェクションリスクが残る）

正しい記述例（推奨）:
```
subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window}", text, "Enter"], check=True)
```
`shlex.quote` の言及は全て削除すること。

---

### Issue B — `handle_message` の `try/finally` 構造が未定義であり、Coder が実装できない

DD では「ステップ 9〜13 で例外が発生した場合、`finally` 節で `is_processing` を解除する」と記載されているが、`try:` ブロックの開始位置が明示されていない。

具体的に不明な点:
- `try:` はステップ 6 の `with state.lock:` ブロックの外に置くのか、ステップ 7 以降に置くのか。
- `lock` ブロック内で `is_processing = True` を設定した後、lock を抜けてから `try/finally` に入る構造を想定しているのか。

`is_processing = True` を設定した後のどこかで `RuntimeError`（tmux subprocess 失敗）や `SlackApiError` が発生した場合、`finally` が走らなければチャンネルが永久ロックされる。

修正依頼: DD に以下の疑似コードレベルの構造を追記すること。
```python
with state.lock:
    # step 6: check / set is_processing, generation, own_gen
    ...
try:
    # steps 7–13
    ...
finally:
    with state.lock:
        if state.generation == own_gen:
            state.is_processing = False
```
`try:` の開始位置（`with state.lock:` の直後）を明記すること。

---

### Issue C — `handle_reset` の `state` 変数が束縛されないまま参照される

DD の `handle_reset` 処理手順に以下が記載されている:

> `channel_id not in _channel_states` なら return
> `with state.lock:` 内で ...

`_channel_states` から `state` を取得する `state = _channel_states[channel_id]` の行が明示されていない。Coder は `state` が undefined のまま `with state.lock:` を書くことになる。

修正依頼: `channel_id not in _channel_states` チェックの直後に `state = _channel_states[channel_id]` の代入行を追加すること。

---

### Issue D — `_watch_for_response` 内で参照する `state` の取得方法が未定義

関数シグネチャ `_watch_for_response(response_file, channel_id, thread_ts, own_gen, client)` には `state` が含まれない。しかし DD の本文は `state.generation`、`state.lock`、`state.is_processing` を参照している。

`state` を `_channel_states[channel_id]` から取得することは推測可能だが、DD に明示がない。また、`_channel_states` はモジュールグローバルであるため、スレッド内からアクセスすることの安全性（初期化後は読み取り専用であることの明記）が必要である。

修正依頼: 関数の先頭処理として `state = _channel_states[channel_id]` を取得することを DD に明記すること。また「`_channel_states` 辞書自体は初期化後に変更されないため、lock なしで参照して安全」である旨を注記すること。

---

### Issue E — settle 判定でサイズが変化していた場合の動作が未定義

DD の settle 判定:
> ファイルが見つかった時点でサイズを記録し、`SETTLE_DURATION` 秒後に再取得して同じであれば書き込み完了とみなす

「同じでなければ」どうするかが記載されていない。Coder の選択肢は複数ある:
- 再度 `SETTLE_DURATION` 秒待って再チェック（ループ）
- ポーリングループ先頭に戻る（次の `POLL_INTERVAL` 待ちが挿入される）
- エラーとして扱う

タイムアウトまでの残り時間との関係も不明確である（settle ループ中にタイムアウトを超過した場合の動作）。

修正依頼: settle 判定に「サイズが変化していた場合はポーリングループ先頭に戻る（サイズ変化している間は検出済みとみなさない）」または「再度 `SETTLE_DURATION` 秒待ってリチェックする」のいずれかを明記すること。またタイムアウト判定はポーリングループの先頭で行い、settle 待ち中もタイムアウトカウントを進めることを明記すること。

---

Requests to Planner:

1. **DD `src/tmux_handler.py` `send_input` セクション**: `shlex.quote` に関する記述をすべて削除し、`subprocess.run` リスト形式で `text` をそのままリスト要素として渡すことを明記すること（Issue A）。

2. **DD `src/bot.py` `handle_message` セクション**: ステップ 6 の後に `try:` ブロックを開始し、ステップ 7〜13 を囲む `try/finally` 構造の擬似コードを追記すること（Issue B）。

3. **DD `src/bot.py` `handle_reset` セクション**: 認可チェック直後に `state = _channel_states[channel_id]` を代入する手順を追記すること（Issue C）。

4. **DD `src/bot.py` `_watch_for_response` セクション**: 関数先頭で `state = _channel_states[channel_id]` を取得することを明記し、`_channel_states` がスレッドセーフに参照可能な理由を注記すること（Issue D）。

5. **DD `src/bot.py` `_watch_for_response` セクション**: settle 判定においてサイズ不一致時の動作（ループ継続 or 再待ち）と、タイムアウトカウントとの関係を明記すること（Issue E）。

---

## Design Review (Pass 2)

Status: ❌ Rejected

### Verification of 5 Prior Fixes

| Fix | Result | Notes |
|---|---|---|
| A (`send_input`: `shlex.quote` 削除、リスト形式で `text` を直接渡す) | ✅ 修正済み | 実装コード例と禁止理由も明記されている |
| B (`handle_message`: `try:` が `with state.lock:` 直後から開始、`finally` で lock 取得後世代チェック) | ✅ 修正済み | 疑似コードが正確に構造を示しており Coder が迷わない |
| C (`handle_reset`: `state = _channel_states[channel_id]` が `with state.lock:` の前に明示) | ✅ 修正済み | 認可チェック直後に代入行が追記されている |
| D (`_watch_for_response`: 関数先頭で `state` 取得、`_channel_states` が読み取り専用である旨の注記) | ✅ 修正済み | 両方とも明記されている |
| E (settle 判定: サイズ不一致→ポーリングループ先頭へ戻る、タイムアウトはループ先頭で毎回チェック) | ✅ 修正済み | settle 待ち中のタイムアウト継続も明記されている |

### New Issue Found

#### Issue F — `handle_message` の `try/finally` がウォッチャースレッド起動成功後も `is_processing` を解除してしまう

DD に追記された `try/finally` 構造（ステップ 7〜13 を囲む）は、ステップ 13 のウォッチャースレッド起動（`Thread.start()`）を `try:` ブロック内に含めている。この構造には以下の問題がある。

`Thread.start()` が**成功した場合**、`handle_message` の `finally` 節は必ず実行され、`is_processing = False` を設定する。しかしウォッチャースレッドはこの時点でまだ動作中であり、スレッド自身の `finally` 節がのちに `is_processing` を解除するはずである。

結果として、ウォッチャースレッドが応答ファイルを待っている間に `is_processing` が `False` になり、同一チャンネルへの新しいメッセージが受け付けられてしまう（2つの並行ウォッチャーが同一チャンネルで走る状態になる）。

**具体的なシナリオ:**

1. メッセージ受信 → `is_processing = True`、`generation = 1`、ウォッチャー起動
2. `Thread.start()` 成功 → `handle_message` の `finally` 実行 → `is_processing = False`
3. 別のメッセージ受信 → `is_processing = False` なので受付 → `generation = 2`、新ウォッチャー起動
4. 世代 1 のウォッチャーは `state.generation (2) != own_gen (1)` で失効終了 → `is_processing` 解除はスキップ
5. 世代 2 のウォッチャーが `is_processing = True` のまま動作 → 正常だが、世代 1 の応答ファイルが残留する可能性あり

`/reset` コマンドによる意図的な中断とは異なり、これは通常メッセージ処理の中で自動的に発生する。

**修正依頼:**

DD `src/bot.py` `handle_message` セクションの `try/finally` 構造を以下のいずれかに修正すること:

- **推奨（フラグ方式）**: `try:` ブロック内でウォッチャースレッドを起動する直前に `watcher_started = False` フラグを宣言し、`Thread.start()` 成功直後に `watcher_started = True` とする。`finally` 節では `not watcher_started` の場合のみ `is_processing` を解除する。

  ```python
  watcher_started = False
  try:
      # ステップ 7〜12
      ...
      threading.Thread(...).start()
      watcher_started = True  # ← 起動成功後にセット
  finally:
      if not watcher_started:
          with state.lock:
              if state.generation == own_gen:
                  state.is_processing = False
  ```

- **代替（ウォッチャー委譲方式）**: `is_processing` の解除責任を常にウォッチャースレッドに持たせ、`handle_message` の `finally` 節では解除を行わない。ただしこの場合、`Thread.start()` 前に例外が発生した際は別途解除処理が必要になる。

Requests to Planner:

- **DD `src/bot.py` `handle_message` セクション**: `try/finally` の疑似コードを上記推奨（フラグ方式）に修正し、ウォッチャースレッド起動成功後は `handle_message` 側から `is_processing` を解除しないことを明記すること（Issue F）。

---

## Design Review (Pass 3)

Status: ✅ Approved

### Verification of Issue F Fix

| Item | Result | Notes |
|---|---|---|
| F (`handle_message`: `watcher_started = False` before `try:`, set to `True` after `Thread.start()`, `finally` only clears when `not watcher_started`) | ✅ 修正済み | DD lines 267–287 に正確な構造が示されている。ウォッチャー起動成功後は `handle_message` 側での解除を行わないことも明記されている |

### Full Checklist Results

| # | Item | Result |
|---|---|---|
| 1 | BD が要件・変更理由・影響スコープを明示している | ✅ |
| 2 | DD が変更ファイルを全て正確なパスで列挙している | ✅ |
| 3 | DD が関数名と振る舞いを具体的に規定している（曖昧な記述なし） | ✅ |
| 4 | 既存機能（`app.py`）を破壊的に削除せず残置している | ✅ |
| 5 | CLAUDE.md の設計原則と矛盾しない（ファイル境界・ChannelState・ストリーム解析なし） | ✅ |
| 6 | 不必要な抽象化・過剰設計がない | ✅ |
| 7 | ChannelState 不変条件（ロック内 R-M-W、`finally` での解除、世代チェック）が保たれている | ✅ |
| 8 | Slack API 冪等性：`chat_postMessage` / `files_upload_v2` は1回のみ送信し送信後リトライなし | ✅ |
| 9 | 応答ファイルパスがチャンネルと世代で一意（`{channel_id}_{gen}.txt`） | ✅ |
| 10 | `thread_ts` がリクエスト単位（世代単位）でウォッチャースレッドへ引数渡しされる | ✅ |
| 11 | 長文閾値が 3000 文字である | ✅ |
| 12 | `files_upload_v2` を使用（非推奨の `files.upload` ではない） | ✅ |
| 13 | `tmux send-keys` の呼び出しが `shell=False` リスト形式、`shlex.quote` なし | ✅ |
| 14 | チャンネル認可チェックが `handle_message` の先頭ステップで行われる | ✅ |
| 15 | `/reset` が `generation += 1` と `is_processing = False` を `state.lock` 内でアトミックに実行する | ✅ |

### Notes for Coder

- `handle_message` の `watcher_started` フラグ：`Thread.start()` は通常失敗しないが、OSリソース枯渇等のまれな例外でも `finally` が正しく `is_processing` を解除するよう、DD 指定の構造を忠実に実装すること。
- `send_input` の `text` はリスト要素としてそのまま渡すこと。`shlex.quote` は適用しない（DD に明記済み）。
- `files_upload_v2` の `filetype="markdown"` はSlack APIによって実際にサポートされているか確認すること。サポートされていない場合は `filetype="post"` にフォールバックするか、`filetype` を省略すること（DD の指定に従い1回送信、リトライなし）。
- `_channel_states` はモジュール初期化後に辞書エントリが追加・削除されないこと。スレッドから lock なしで参照するための前提であるため、実行時に辞書を変更するコードを書かないこと。

---

## Design Review (Final)

Status: ✅ Approved

### Scope

This is a full scratch review of the entire DD in its current state. All prior review passes (Pass 1, Pass 2, Pass 3, and CHANNEL_MAP_JSON Addendum) were re-verified from the final text of the DD. No prior review assumption is inherited.

### Key Area Verification

| Area | Check | Result |
|---|---|---|
| settle判定 | size1 before sleep → SETTLE_DURATION sleep → generation recheck (4c) → timeout recheck (4d) → re-exists check (4e) → size2 compare → size mismatch loops back with POLL_INTERVAL → settle-delete loops back | ✅ |
| ChannelState invariants | generation + is_processing R-M-W under `state.lock`; `is_processing` cleared in `finally` with `state.generation == own_gen` guard; `watcher_started` flag correctly separates `handle_message` vs `_watch_for_response` finally responsibilities | ✅ |
| Response file path | `claude_bot_response_{channel_id}_{gen}.txt` — unique per channel and per generation; prev-gen cleanup on step 8 scoped to same channel_id | ✅ |
| `_build_prompt` | Single `response_file` path (no staging); Japanese; final answer only; no drafts; TOP PRIORITY; explicit path; prohibits writing elsewhere | ✅ |
| Slack API non-idempotency | `send_long_text` sends exactly once; `SlackApiError` propagated without retry; no post-send retry loop exists anywhere in DD | ✅ |
| CHANNEL_MAP_JSON | Loaded from env in `_load_channel_map()`; `ValueError` on parse failure causes startup crash (no silent boot); `cwd`/`tmp` pre-existence documented; not hardcoded | ✅ |
| Thread replies | `thread_ts = event.get("thread_ts") or event["ts"]` per-request; passed per-generation to watcher as arg; used in `send_long_text`; slash command `/reset` has no originating `thread_ts` — posts to channel directly (correct) | ✅ |
| Unregistered channel | `handle_message` step 2: `channel_id not in _channel_states` → rejection reply → `return`; no processing proceeds | ✅ |
| `/reset` scope | Own channel: uses `command["channel_id"]`. All channels: loops all `_channel_states`. Both do `generation += 1` + `is_processing = False` atomically under each `state.lock` | ✅ |
| DD function-level specification | All functions in all 5 modules (`config.py`, `channel_state.py`, `tmux_handler.py`, `file_handler.py`, `bot.py`) have named parameters, return types, and step-by-step behavior; no vague descriptions | ✅ |

### Review Checklist (DD-applicable items)

| Checklist Item | Result |
|---|---|
| 1.3 No user-supplied file paths; path constructed server-side from `state.tmp` + channel_id + gen | ✅ |
| 2.1 Response file relay design intact | ✅ |
| 2.2 `[TOP PRIORITY]` + sole output channel instruction in `_build_prompt` | ✅ |
| 2.3 Path uses `{channel_id}_{gen}` — collision-free across concurrent requests | ✅ |
| 3.1 ChannelState: R-M-W under lock, finally-block clearing, generation-matched guard | ✅ |
| 3.3 `/reset` increments generation AND clears `is_processing` atomically | ✅ |
| 4.2 `thread_ts` passed per-generation to watcher (not per-channel) | ✅ |
| 5.2 No message splitting — below threshold posts whole, above uploads as file snippet; no code-fence split risk | ✅ |
| 5.3 3000 char threshold | ✅ |
| 6.1 Non-idempotent calls sent exactly once, no post-send retry | ✅ |
| 7.1 No reserved window management issues; bot window not named in CHANNEL_MAP | ✅ |

### Notes for Coder

- `handle_message` step 8 (prev-gen cleanup) runs `os.remove` outside the lock. This is safe because the file is only written by Claude Code (external process), and the watcher for the previous generation will already have exited or reached its finally block before generation is incremented. No race condition with step 8.
- `handle_reset` reads `state.generation` outside the lock to compose the reply string (`generation={state.generation}`). This is a benign display-only race — the value shown may be stale by nanoseconds in extreme concurrent-reset scenarios, but it has no effect on correctness or ChannelState invariants. No DD change needed.
- `files_upload_v2` with `filetype="markdown"`: confirm this value is accepted by the Slack API before shipping. If rejected, use `filetype="post"` or omit `filetype`. Either fallback is compliant with the single-send, no-retry constraint.
- `_channel_states` dict entries must not be added or removed after initialization. This is the precondition for lock-free reads from watcher threads. Do not write code that modifies the dict at runtime.
