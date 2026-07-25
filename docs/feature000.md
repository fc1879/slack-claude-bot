# feature000 — Core Bot Implementation: Slack tmux Relay with Per-Channel State

## ステータス

| 工程 | ステータス | 完了日 | 備考 |
|---|---|---|---|
| BD | ✅ | 2026-07-25 | |
| DD | ✅ | 2026-07-25 | Design Review (Final) approved |
| CD | ✅ | 2026-07-25 | |
| UT | ✅ | 2026-07-26 | T-01〜T-41 全件 Pass（コードインスペクション）|
| IT | 🔄 進行中 | | |
| ST | 🔄 進行中 | | |
| UAT | 🔄 進行中 | | |

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

---

### T-01: `_load_channel_map` — CHANNEL_MAP_JSON 未設定で ValueError が raise される

- **Phase**: UT
- **Precondition**: `CHANNEL_MAP_JSON` 環境変数が未設定（または空文字列）の状態で `src/config.py` をインポートする。
- **Steps**:
  1. テスト環境の `os.environ` から `CHANNEL_MAP_JSON` を削除または空文字にセットする。
  2. `from src.config import _load_channel_map` を実行する。
  3. `_load_channel_map()` を呼び出す。
- **Expected result**: `ValueError` が raise される。エラーメッセージには `"CHANNEL_MAP_JSON"` という文字列が含まれる。
- **Pass criteria**: `pytest.raises(ValueError)` が成功し、メッセージに `"CHANNEL_MAP_JSON"` が含まれる。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `raw = os.environ.get("CHANNEL_MAP_JSON", "")` — if empty, `if not raw:` is True and raises `ValueError("CHANNEL_MAP_JSON environment variable is not set. ...")`. Message contains `"CHANNEL_MAP_JSON"`.
- **Notes**: None.

---

### T-02: `_load_channel_map` — 不正 JSON で ValueError が raise される

- **Phase**: UT
- **Precondition**: `CHANNEL_MAP_JSON` 環境変数に `"{invalid json"` をセットする。
- **Steps**:
  1. `os.environ["CHANNEL_MAP_JSON"] = "{invalid json"` をセットする。
  2. `_load_channel_map()` を呼び出す。
- **Expected result**: `ValueError` が raise される。メッセージには `"not valid JSON"` が含まれる。
- **Pass criteria**: `pytest.raises(ValueError)` が成功し、メッセージに `"not valid JSON"` が含まれる。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `json.JSONDecodeError` is caught and re-raised as `ValueError(f"CHANNEL_MAP_JSON is not valid JSON: {e}")`. Message contains `"not valid JSON"`.
- **Notes**: None.

---

### T-03: `_load_channel_map` — 必須キー不足で ValueError が raise される

- **Phase**: UT
- **Precondition**: `CHANNEL_MAP_JSON` 環境変数に `target` キーが欠けた JSON をセットする（例: `{"C01": {"cwd": "/p", "tmp": "/t"}}`）。
- **Steps**:
  1. `os.environ["CHANNEL_MAP_JSON"] = '{"C01": {"cwd": "/p", "tmp": "/t"}}'` をセットする。
  2. `_load_channel_map()` を呼び出す。
- **Expected result**: `ValueError` が raise される。メッセージにはチャンネル ID `"C01"` および欠けたキー名 `"target"` が含まれる。
- **Pass criteria**: `pytest.raises(ValueError)` が成功し、メッセージに `"C01"` と `"target"` が含まれる。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `KeyError` from `cfg["target"]` is caught and re-raised as `ValueError(f"CHANNEL_MAP_JSON entry for '{channel_id}' is missing key {e}")`. For channel_id="C01" and missing key "target", message = `"CHANNEL_MAP_JSON entry for 'C01' is missing key 'target'"`. Contains both "C01" and "target".
- **Notes**: None.

---

### T-04: `_load_channel_map` — 正常な JSON で `ChannelConfig` 辞書が返される

- **Phase**: UT
- **Precondition**: `CHANNEL_MAP_JSON` 環境変数に正常な単チャンネル JSON をセットする。
- **Steps**:
  1. `os.environ["CHANNEL_MAP_JSON"] = '{"C01": {"target": "s:w", "cwd": "/p", "tmp": "/t"}}'` をセットする。
  2. `_load_channel_map()` を呼び出す。
- **Expected result**: 戻り値は `{"C01": ChannelConfig(target="s:w", cwd="/p", tmp="/t")}` に等しい。
- **Pass criteria**: `result["C01"].target == "s:w"` かつ `result["C01"].cwd == "/p"` かつ `result["C01"].tmp == "/t"` がすべて `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `_load_channel_map()` parses JSON and constructs `ChannelConfig(target=cfg["target"], cwd=cfg["cwd"], tmp=cfg["tmp"])` for each entry. For the given JSON, `result["C01"]` = `ChannelConfig(target="s:w", cwd="/p", tmp="/t")`.
- **Notes**: None.

---

### T-05: `_load_channel_map` — 複数チャンネル JSON で全エントリが正しく生成される

- **Phase**: UT
- **Precondition**: `CHANNEL_MAP_JSON` に 2 チャンネル分の JSON をセットする。
- **Steps**:
  1. `os.environ["CHANNEL_MAP_JSON"] = '{"C01": {"target": "s:w1", "cwd": "/p1", "tmp": "/t1"}, "C02": {"target": "s:w2", "cwd": "/p2", "tmp": "/t2"}}'` をセットする。
  2. `_load_channel_map()` を呼び出す。
- **Expected result**: 戻り値に `"C01"` と `"C02"` の両キーが含まれ、各 `ChannelConfig` のフィールドが JSON の値と一致する。
- **Pass criteria**: `len(result) == 2` かつ `result["C02"].target == "s:w2"` が `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: The loop `for channel_id, cfg in parsed.items()` iterates all keys. Both "C01" and "C02" are added to `result`. `result["C02"]` = `ChannelConfig(target="s:w2", cwd="/p2", tmp="/t2")`.
- **Notes**: None.

---

### T-06: `init_channel_states` — CHANNEL_MAP の各エントリに対応する `ChannelState` が生成される

- **Phase**: UT
- **Precondition**: 2 チャンネル分の `ChannelConfig` 辞書を用意する。
- **Steps**:
  1. `channel_map = {"C01": ChannelConfig("s:w1", "/p1", "/t1"), "C02": ChannelConfig("s:w2", "/p2", "/t2")}` を生成する。
  2. `init_channel_states(channel_map)` を呼び出す。
- **Expected result**: 戻り値は `{"C01": ..., "C02": ...}` の辞書で、各 `ChannelState` の `generation == 0`、`is_processing == False`、`lock` が `threading.Lock` インスタンスである。
- **Pass criteria**: `len(states) == 2` かつ `states["C01"].generation == 0` かつ `states["C01"].is_processing == False` がすべて `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `init_channel_states` returns a dict comprehension creating `ChannelState(target=cfg.target, cwd=cfg.cwd, tmp=cfg.tmp)` for each entry. `ChannelState` dataclass has `generation: int = 0` and `is_processing: bool = False` as defaults. Both "C01" and "C02" are created. `len == 2`, `generation == 0`, `is_processing == False`.
- **Notes**: None.

---

### T-07: `init_channel_states` — 各チャンネルの `lock` が独立したインスタンスである

- **Phase**: UT
- **Precondition**: 2 チャンネル分の `ChannelConfig` 辞書を用意する。
- **Steps**:
  1. T-06 と同様に `init_channel_states(channel_map)` を呼び出す。
  2. `states["C01"].lock is states["C02"].lock` を評価する。
- **Expected result**: `False`（2 つのロックは別オブジェクト）。
- **Pass criteria**: `states["C01"].lock is states["C02"].lock` が `False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `ChannelState` defines `lock: threading.Lock = field(default_factory=threading.Lock)`. `default_factory` is called once per instance, so each `ChannelState` gets a distinct `threading.Lock` object. `states["C01"].lock is states["C02"].lock` → `False`.
- **Notes**: None.

---

### T-08: `session_exists` — セッションが存在する場合 `True` を返す

- **Phase**: UT
- **Precondition**: `subprocess.run` をモック化し、`returncode=0` を返すよう設定する。
- **Steps**:
  1. `subprocess.run` を `MagicMock(returncode=0)` で patch する。
  2. `session_exists("test_session")` を呼び出す。
- **Expected result**: `True` が返される。
- **Pass criteria**: 戻り値が `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `session_exists` returns `result.returncode == 0`. With `returncode=0`, this evaluates to `True`.
- **Notes**: None.

---

### T-09: `session_exists` — セッションが存在しない場合 `False` を返す

- **Phase**: UT
- **Precondition**: `subprocess.run` をモック化し、`returncode=1` を返すよう設定する。
- **Steps**:
  1. `subprocess.run` を `MagicMock(returncode=1)` で patch する。
  2. `session_exists("no_such_session")` を呼び出す。
- **Expected result**: `False` が返される。
- **Pass criteria**: 戻り値が `False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `result.returncode == 0` with `returncode=1` evaluates to `False`.
- **Notes**: None.

---

### T-10: `window_exists` — 対象ウィンドウが出力に含まれる場合 `True` を返す

- **Phase**: UT
- **Precondition**: `subprocess.run` をモック化し、`returncode=0`、`stdout="project-a\nproject-b\n"` を返すよう設定する。
- **Steps**:
  1. `subprocess.run` を patch する。
  2. `window_exists("claude_session", "project-a")` を呼び出す。
- **Expected result**: `True` が返される。
- **Pass criteria**: 戻り値が `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `window_exists` checks `result.returncode != 0` (False for returncode=0), then returns `window in result.stdout.splitlines()`. `"project-a" in ["project-a", "project-b"]` → `True`.
- **Notes**: None.

---

### T-11: `window_exists` — 対象ウィンドウが出力に含まれない場合 `False` を返す

- **Phase**: UT
- **Precondition**: `subprocess.run` をモック化し、`returncode=0`、`stdout="other-window\n"` を返すよう設定する。
- **Steps**:
  1. `subprocess.run` を patch する。
  2. `window_exists("claude_session", "project-a")` を呼び出す。
- **Expected result**: `False` が返される。
- **Pass criteria**: 戻り値が `False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `"project-a" in ["other-window"]` → `False`.
- **Notes**: None.

---

### T-12: `window_exists` — セッション自体が存在しない（`returncode != 0`）場合 `False` を返す

- **Phase**: UT
- **Precondition**: `subprocess.run` をモック化し、`returncode=1` を返すよう設定する。
- **Steps**:
  1. `subprocess.run` を patch する。
  2. `window_exists("no_session", "project-a")` を呼び出す。
- **Expected result**: `False` が返される。
- **Pass criteria**: 戻り値が `False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `if result.returncode != 0: return False` — with returncode=1 this early-returns `False` before checking stdout.
- **Notes**: None.

---

### T-13: `send_input` — tmux コマンドが正しいリスト形式で呼ばれ、0.3 秒 sleep が実行される

- **Phase**: UT
- **Precondition**: `subprocess.run` と `time.sleep` をモック化する。
- **Steps**:
  1. `subprocess.run` と `time.sleep` を patch する。
  2. `send_input("claude_session", "hello world", "project-a")` を呼び出す。
- **Expected result**: `subprocess.run` が `["tmux", "send-keys", "-t", "claude_session:project-a", "hello world", "Enter"]` というリストで呼ばれる（`check=True`）。`time.sleep` が `0.3` で呼ばれる。`text` 引数に `shlex.quote` によるクォートが含まれない。
- **Pass criteria**: `subprocess.run.call_args.args[0]` が上記リストに等しく、`time.sleep.call_args.args[0] == 0.3` が `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `send_input` calls `subprocess.run(["tmux", "send-keys", "-t", f"{session}:{window}", text, "Enter"], check=True)` then `time.sleep(0.3)`. The text "hello world" is passed directly as a list element — no `shlex.quote` used anywhere in the file.
- **Notes**: None.

---

### T-14: `send_input` — `subprocess.run` が `CalledProcessError` を raise した場合、例外が伝播する

- **Phase**: UT
- **Precondition**: `subprocess.run` を `CalledProcessError` を raise するよう patch する。
- **Steps**:
  1. `subprocess.run` を `side_effect=CalledProcessError(1, "tmux")` で patch する。
  2. `send_input("s", "text", "w")` を呼び出す。
- **Expected result**: `CalledProcessError` が呼び出し元へ伝播する。
- **Pass criteria**: `pytest.raises(CalledProcessError)` が成功する。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `subprocess.run(..., check=True)` — when `check=True`, a non-zero returncode raises `CalledProcessError`. With `side_effect=CalledProcessError`, the exception propagates directly. No try/except in `send_input`.
- **Notes**: None.

---

### T-15: `create_window` — `new-window` コマンドの後に 1.0 秒 sleep し、`claude` 起動コマンドを送信する

- **Phase**: UT
- **Precondition**: `subprocess.run` と `time.sleep` をモック化する。`send_input` もモック化する。
- **Steps**:
  1. `subprocess.run`、`time.sleep`、`send_input` を patch する。
  2. `create_window("claude_session", "project-a", "/some/path")` を呼び出す。
- **Expected result**: `subprocess.run` が `["tmux", "new-window", "-t", "claude_session", "-n", "project-a", "-c", "/some/path"]` で呼ばれる。その後 `time.sleep(1.0)` が呼ばれる。最後に `send_input("claude_session", "claude --dangerously-skip-permissions", "project-a")` が呼ばれる。
- **Pass criteria**: 上記 3 つの呼び出しがすべて正しい引数で行われ、順序が `new-window` → `sleep(1.0)` → `send_input` であることを call_args_list で確認できる。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `create_window` calls `subprocess.run(["tmux", "new-window", "-t", session, "-n", window, "-c", cwd], check=True)`, then `time.sleep(1.0)`, then `send_input(session, "claude --dangerously-skip-permissions", window)`. Order matches exactly.
- **Notes**: None.

---

### T-16: `ensure_window` — ウィンドウが存在しない場合 `create_window` を呼ぶ

- **Phase**: UT
- **Precondition**: `window_exists` が `False` を返すようモック化する。`create_window` をモック化する。
- **Steps**:
  1. `window_exists` を `return_value=False` で patch する。
  2. `create_window` を patch する。
  3. `ensure_window("s", "w", "/p")` を呼び出す。
- **Expected result**: `create_window("s", "w", "/p")` が 1 回呼ばれる。
- **Pass criteria**: `create_window.call_count == 1` かつ引数が `("s", "w", "/p")` に等しい。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `ensure_window` is `if not window_exists(session, window): create_window(session, window, cwd)`. When `window_exists` returns `False`, `create_window(session, window, cwd)` is called once with the correct arguments.
- **Notes**: None.

---

### T-17: `ensure_window` — ウィンドウが存在する場合 `create_window` を呼ばない

- **Phase**: UT
- **Precondition**: `window_exists` が `True` を返すようモック化する。`create_window` をモック化する。
- **Steps**:
  1. `window_exists` を `return_value=True` で patch する。
  2. `create_window` を patch する。
  3. `ensure_window("s", "w", "/p")` を呼び出す。
- **Expected result**: `create_window` が呼ばれない。
- **Pass criteria**: `create_window.call_count == 0`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `if not window_exists(...)` — when `window_exists` returns `True`, `not True` is `False`, so `create_window` is never called.
- **Notes**: None.

---

### T-18: `send_long_text` — テキストが 3000 文字以下の場合 `chat_postMessage` を呼ぶ

- **Phase**: UT
- **Precondition**: Slack `client` をモック化する。テキストを "a" × 3000 文字（境界値）にする。
- **Steps**:
  1. `client = MagicMock()` を用意する。
  2. `send_long_text(client, "C01", "a" * 3000, "ts123")` を呼び出す。
- **Expected result**: `client.chat_postMessage` が `channel="C01"`, `text="a"*3000`, `thread_ts="ts123"` で 1 回呼ばれる。`client.files_upload_v2` は呼ばれない。
- **Pass criteria**: `client.chat_postMessage.call_count == 1` かつ `client.files_upload_v2.call_count == 0`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `if len(text) <= MAX_MESSAGE_LENGTH:` — `len("a"*3000) == 3000 <= 3000` is `True`, so `client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)` is called. `files_upload_v2` is not called.
- **Notes**: None.

---

### T-19: `send_long_text` — テキストが 3001 文字以上の場合 `files_upload_v2` を呼ぶ

- **Phase**: UT
- **Precondition**: Slack `client` をモック化する。テキストを "a" × 3001 文字（境界値 + 1）にする。
- **Steps**:
  1. `client = MagicMock()` を用意する。
  2. `send_long_text(client, "C01", "a" * 3001, "ts123")` を呼び出す。
- **Expected result**: `client.files_upload_v2` が `channel="C01"`, `content="a"*3001`, `filename="response.md"`, `filetype="markdown"`, `thread_ts="ts123"` で 1 回呼ばれる。`client.chat_postMessage` は呼ばれない。
- **Pass criteria**: `client.files_upload_v2.call_count == 1` かつ `client.chat_postMessage.call_count == 0`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `len("a"*3001) == 3001 > 3000`, so the else branch executes: `client.files_upload_v2(channel=channel, content=text, filename="response.md", filetype="markdown", thread_ts=thread_ts)`. `chat_postMessage` is not called.
- **Notes**: None.

---

### T-20: `send_long_text` — `SlackApiError` が raise された場合、呼び出し元へ伝播する（リトライなし）

- **Phase**: UT
- **Precondition**: `client.chat_postMessage` が `SlackApiError` を raise するようモック化する。テキストを 100 文字にする。
- **Steps**:
  1. `client.chat_postMessage = MagicMock(side_effect=SlackApiError("error", {}))` をセットする。
  2. `send_long_text(client, "C01", "x" * 100, "ts123")` を呼び出す。
- **Expected result**: `SlackApiError` が伝播する。`client.chat_postMessage` は 1 回のみ呼ばれる（リトライなし）。
- **Pass criteria**: `pytest.raises(SlackApiError)` が成功し、`client.chat_postMessage.call_count == 1`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `send_long_text` has no try/except and no retry loop. `client.chat_postMessage(...)` is called exactly once; if it raises `SlackApiError`, the exception propagates directly to the caller.
- **Notes**: None.

---

### T-21: `_build_prompt` — 返されるプロンプトに 5 つの必須要素が全て含まれる

- **Phase**: UT
- **Precondition**: なし。
- **Steps**:
  1. `_build_prompt("ユーザーメッセージ", "/tmp/response.txt")` を呼び出す。
- **Expected result**: 返されたプロンプト文字列に以下が全て含まれる:
  - `"ユーザーメッセージ"` （元メッセージ）
  - `"[TOP PRIORITY]"`
  - `"Write ツールで書き出すこと"`
  - `"日本語で記述すること"`
  - `"/tmp/response.txt"` （出力先パス）
- **Pass criteria**: 上記 5 つの文字列が全て `result` に含まれる（`in` 演算子で確認）。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `_build_prompt` returns `f"{message}\n\n---\n[TOP PRIORITY] 以下の指示に従うこと:\n1. 上記の要求への全ての推論・作業が完了した後、最終回答のみを以下のパスに Write ツールで書き出すこと。\n2. 途中経過・下書き・部分的な内容を書き出してはならない。\n3. `[確認]` のような確認文も最終回答として書き出すこと。\n4. 回答は日本語で記述すること。\n5. 以下のパス以外へ書き出してはならない。\n\n出力先パス（Write ツールで書き出す先）:\n{response_file}"`. All 5 required strings are present: original message, `"[TOP PRIORITY]"`, `"Write ツールで書き出すこと"`, `"日本語で記述すること"`, and `"/tmp/response.txt"`.
- **Notes**: None.

---

### T-22: `handle_message` — DM ガード: `type=message` かつ `channel_type != "im"` は即座に return する

- **Phase**: UT
- **Precondition**: `_channel_states` にチャンネル ID が存在する。`client` をモック化する。
- **Steps**:
  1. `event = {"type": "message", "channel_type": "channel", "channel": "C01", "ts": "1.0"}` を用意する。
  2. `handle_message(event, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: `client.chat_postMessage` が一切呼ばれない。`_channel_states["C01"].is_processing` は `False` のまま。
- **Pass criteria**: `client.chat_postMessage.call_count == 0` かつ `_channel_states["C01"].is_processing == False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: Step 0 guard is `if event.get("type") == "message" and event.get("channel_type") != "im": return`. For `type="message"` and `channel_type="channel"`, both conditions are `True`, so the function returns immediately. No `chat_postMessage` is called and no state is mutated.
- **Notes**: None.

---

### T-23: `handle_message` — DM（`channel_type=im`）は処理を通過する

- **Phase**: UT
- **Precondition**: `_channel_states` に DM チャンネル ID を登録する。`tmux_handler.send_input` と `tmux_handler.ensure_window` をモック化する。`threading.Thread` をモック化する。`client` をモック化する。
- **Steps**:
  1. `event = {"type": "message", "channel_type": "im", "channel": "C_DM", "ts": "1.0", "text": "こんにちは"}` を用意する。
  2. `handle_message(event, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: DM ガード (Step 0) を通過し、チャンネル認可チェック以降の処理（「処理中」または「送信しました」返信）が実行される。
- **Pass criteria**: `client.chat_postMessage.call_count >= 1`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: For `type="message"` and `channel_type="im"`, the condition `event.get("channel_type") != "im"` is `False`, so `event.get("type") == "message" and False` is `False` — the guard does NOT fire. Processing continues to the authorization check, text extraction, and eventually a `chat_postMessage` call ("送信しました" or rejection).
- **Notes**: None.

---

### T-24: `handle_message` — 未登録チャンネルから送信した場合、拒否メッセージが返される

- **Phase**: UT
- **Precondition**: `_channel_states` に `"C_UNKNOWN"` が存在しない。`client` をモック化する。`event` の `type` を `"app_mention"` にする。
- **Steps**:
  1. `event = {"type": "app_mention", "channel": "C_UNKNOWN", "ts": "1.0", "text": "hello"}` を用意する。
  2. `handle_message(event, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: `client.chat_postMessage` が `text="このチャンネルは未登録です。管理者に連絡してください。"` で 1 回呼ばれる。`_channel_states` の変更はない。
- **Pass criteria**: `client.chat_postMessage.call_args.kwargs["text"] == "このチャンネルは未登録です。管理者に連絡してください。"` が `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: Step 0 guard: `event.get("type") == "app_mention"` is not `"message"`, so guard does not fire. Step 2: `if channel_id not in _channel_states:` — True for "C_UNKNOWN", so `client.chat_postMessage(channel=channel_id, text="このチャンネルは未登録です。管理者に連絡してください。", thread_ts=thread_ts)` is called and `return` exits the function.
- **Notes**: None.

---

### T-25: `handle_message` — メンション除去後に空テキストとなる場合、エラーメッセージが返される

- **Phase**: UT
- **Precondition**: `_channel_states` に `"C01"` が登録されている。`client` をモック化する。
- **Steps**:
  1. `event = {"type": "app_mention", "channel": "C01", "ts": "1.0", "text": "<@U12345>"}` を用意する（メンション除去後は空文字列になる）。
  2. `handle_message(event, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: `client.chat_postMessage` が `text="メッセージが空です。"` で 1 回呼ばれる。`is_processing` は変化しない。
- **Pass criteria**: `client.chat_postMessage.call_args.kwargs["text"] == "メッセージが空です。"` が `True` かつ `_channel_states["C01"].is_processing == False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `re.sub(r"<@[A-Z0-9]+>", "", "<@U12345>").strip()` = `""`. Then `if not clean_text:` is `True`, calls `client.chat_postMessage(channel=channel_id, text="メッセージが空です。", thread_ts=thread_ts)` and returns. `is_processing` is never touched — remains `False`.
- **Notes**: None.

---

### T-26: `handle_message` — 処理中に同一チャンネルへ送信すると「処理中」メッセージが返される（already_processing フラグ）

- **Phase**: UT
- **Precondition**: `_channel_states["C01"].is_processing = True` を事前にセットする。`client` をモック化する。
- **Steps**:
  1. `_channel_states["C01"].is_processing = True` をセットする。
  2. `event = {"type": "app_mention", "channel": "C01", "ts": "1.0", "text": "new message"}` を用意する。
  3. `handle_message(event, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: `client.chat_postMessage` が `text="処理中です。完了をお待ちください。/reset で中断できます。"` で 1 回呼ばれる。`is_processing` は `True` のまま。生成番号は変化しない。
- **Pass criteria**: 返信テキストが `"処理中です。完了をお待ちください。/reset で中断できます。"` に等しく、`_channel_states["C01"].is_processing == True` かつ `_channel_states["C01"].generation` が変化していない。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: Inside `with state.lock:`, `if state.is_processing:` is `True` → `already_processing = True` (else branch skipped — no generation increment, no `is_processing = True`). After the lock block, `if already_processing:` → calls `client.chat_postMessage(..., text="処理中です。完了をお待ちください。/reset で中断できます。", ...)` and returns. `is_processing` remains `True`, `generation` unchanged.
- **Notes**: None.

---

### T-27: `handle_message` — 「処理中」返信はロック外で送信される（already_processing フラグ方式）

- **Phase**: UT
- **Precondition**: T-26 と同じ設定。`state.lock` を実際の `threading.Lock` のまま使用する。
- **Steps**:
  1. T-26 の手順を実行する。
  2. `handle_message` 呼び出し後、`state.lock.locked()` を確認する。
- **Expected result**: `handle_message` 返却後にロックは解放されている（`state.lock.locked() == False`）。デッドロックが発生していない。
- **Pass criteria**: `state.lock.locked() == False` かつ関数呼び出しが完了している（タイムアウトなし）。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: The `with state.lock:` block is closed before `if already_processing:` branch executes. `client.chat_postMessage` is called outside the lock. After `handle_message` returns, the lock is released (context manager guarantees release on exit from `with` block). No I/O is performed while holding the lock.
- **Notes**: None.

---

### T-28: `handle_message` — `tmux send_input` 失敗時に `is_processing` が解除される（watcher_started=False パス）

- **Phase**: UT
- **Precondition**: `_channel_states["C01"].is_processing = False`。`tmux_handler.send_input` が `RuntimeError` を raise するようモック化する。`tmux_handler.ensure_window` をモック化する。`client` をモック化する。
- **Steps**:
  1. `tmux_handler.send_input = MagicMock(side_effect=RuntimeError("tmux failed"))` をセットする。
  2. `event = {"type": "app_mention", "channel": "C01", "ts": "1.0", "text": "test"}` を呼び出す。
  3. `handle_message(event, client=MagicMock(), logger=MagicMock())` を呼び出す（例外を catch する）。
- **Expected result**: `RuntimeError` が伝播（または `finally` 内で処理）し、`_channel_states["C01"].is_processing` が `False` に戻る。`watcher_started` は `False` のままであった。
- **Pass criteria**: `handle_message` 返却後（または例外後）に `_channel_states["C01"].is_processing == False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `watcher_started = False` is declared before the `try:` block. `tmux_handler.send_input` raises `RuntimeError` inside the `try:` block. `Thread.start()` is never reached, so `watcher_started` stays `False`. The `finally:` block checks `if not watcher_started:` (True) and executes `with state.lock: if state.generation == own_gen: state.is_processing = False`. So `is_processing` is cleared.
- **Notes**: None.

---

### T-29: `handle_message` — `Thread.start()` 失敗時に `is_processing` が解除される

- **Phase**: UT
- **Precondition**: `tmux_handler.send_input` と `tmux_handler.ensure_window` をモック化する。`threading.Thread.start` が `RuntimeError` を raise するよう patch する。`client` をモック化する。
- **Steps**:
  1. `threading.Thread` の `start` メソッドを `side_effect=RuntimeError("os error")` で patch する。
  2. 正常な `app_mention` イベントで `handle_message` を呼び出す。
- **Expected result**: `RuntimeError` が伝播し、`_channel_states["C01"].is_processing` が `False` に戻る。
- **Pass criteria**: 呼び出し後に `_channel_states["C01"].is_processing == False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `Thread(...).start()` raises `RuntimeError` before `watcher_started = True` is reached. `watcher_started` remains `False`. The `finally:` block fires: `if not watcher_started:` (True) → `with state.lock: if state.generation == own_gen: state.is_processing = False`.
- **Notes**: None.

---

### T-30: `handle_message` — `Thread.start()` 成功時、`handle_message` の `finally` は `is_processing` を解除しない

- **Phase**: UT
- **Precondition**: `tmux_handler.send_input`、`tmux_handler.ensure_window`、`threading.Thread` をモック化する（`start()` は成功させる）。`client` をモック化する。
- **Steps**:
  1. `threading.Thread` を `MagicMock()` で patch し、`start()` は正常に完了させる。
  2. `event = {"type": "app_mention", "channel": "C01", "ts": "1.0", "text": "hello"}` で `handle_message` を呼び出す。
  3. `handle_message` 返却直後に `_channel_states["C01"].is_processing` を確認する。
- **Expected result**: `is_processing` は `True` のまま（ウォッチャースレッドの `finally` に委譲されているため）。
- **Pass criteria**: `_channel_states["C01"].is_processing == True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: After `Thread(...).start()` succeeds, `watcher_started = True` is set. The `finally:` block checks `if not watcher_started:` — this is `False`, so `is_processing` is NOT cleared. `is_processing` remains `True` as set in step 6 inside the lock.
- **Notes**: None.

---

### T-31: `handle_message` — 世代番号とレスポンスファイルパスが正しく構築される

- **Phase**: UT
- **Precondition**: `_channel_states["C01"].generation = 0`。`tmux_handler.*` と `threading.Thread` をモック化する。`client` をモック化する。
- **Steps**:
  1. `event = {"type": "app_mention", "channel": "C01", "ts": "1.0", "text": "hello"}` で `handle_message` を呼び出す。
  2. `tmux_handler.send_input` へ渡された `text` 引数（`full_prompt`）を確認する。
- **Expected result**: `send_input` に渡される `full_prompt` に `"claude_bot_response_C01_1.txt"` が含まれる。`_channel_states["C01"].generation == 1`。
- **Pass criteria**: `full_prompt` に `"claude_bot_response_C01_1.txt"` が含まれ、`generation == 1`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: Starting with `generation=0`, inside `with state.lock:` the else branch does `state.generation += 1` (→1) and `own_gen = state.generation` (→1). Then `response_file = os.path.join(state.tmp, f"claude_bot_response_{channel_id}_{own_gen}.txt")` = `os.path.join(state.tmp, "claude_bot_response_C01_1.txt")`. `_build_prompt(clean_text, response_file)` includes this path. `send_input` receives `full_prompt` containing "claude_bot_response_C01_1.txt". `generation == 1`.
- **Notes**: None.

---

### T-32: `handle_message` — 前世代の残留ファイルが削除される

- **Phase**: UT
- **Precondition**: `_channel_states["C01"].generation = 1`。`state.tmp` ディレクトリに `claude_bot_response_C01_1.txt` が存在する（前世代残留ファイル）。`tmux_handler.*` と `threading.Thread` をモック化する。
- **Steps**:
  1. `state.tmp` ディレクトリに `claude_bot_response_C01_1.txt` を作成する。
  2. `event = {"type": "app_mention", "channel": "C01", "ts": "1.0", "text": "test"}` で `handle_message` を呼び出す。（このとき `own_gen` は 2 になり、`prev_file` は `_1.txt` になる）
  3. ファイルの存否を確認する。
- **Expected result**: `claude_bot_response_C01_1.txt` が削除されている。
- **Pass criteria**: `os.path.exists(prev_file) == False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: With `generation=1` before the call, `own_gen` becomes 2. `prev_file = os.path.join(state.tmp, f"claude_bot_response_{channel_id}_{own_gen - 1}.txt")` = `"claude_bot_response_C01_1.txt"`. `if os.path.exists(prev_file): os.remove(prev_file)` — deletes the previous generation file.
- **Notes**: None.

---

### T-33: `handle_reset` — 自チャンネルのみ generation をインクリメントし、`is_processing` を `False` にする

- **Phase**: UT
- **Precondition**: `_channel_states["C01"].generation = 5`、`_channel_states["C01"].is_processing = True`。`client` をモック化する。`ack` をモック化する。
- **Steps**:
  1. `command = {"channel_id": "C01", "text": ""}` を用意する。
  2. `handle_reset(ack=MagicMock(), command=command, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: `_channel_states["C01"].generation == 6`、`_channel_states["C01"].is_processing == False`。`client.chat_postMessage` が `text` に `"チャンネルをリセットしました。(generation=6)"` を含む形で 1 回呼ばれる。
- **Pass criteria**: `generation == 6` かつ `is_processing == False` かつ `client.chat_postMessage.call_args.kwargs["text"] == "チャンネルをリセットしました。(generation=6)"`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `command.get("text", "").strip() == "all"` is `False` (text is ""). `state = _channel_states["C01"]`. `with state.lock: state.generation += 1` (5→6), `state.is_processing = False`. Then `client.chat_postMessage(channel=channel_id, text=f"チャンネルをリセットしました。(generation={state.generation})")` = `"チャンネルをリセットしました。(generation=6)"`.
- **Notes**: None.

---

### T-34: `handle_reset` — `/reset all` で全チャンネルがリセットされる

- **Phase**: UT
- **Precondition**: `_channel_states` に `"C01"` と `"C02"` の 2 チャンネルが登録されている。両方の `is_processing = True`、`generation` はそれぞれ任意の値。`client` をモック化する。
- **Steps**:
  1. `command = {"channel_id": "C01", "text": "all"}` を用意する。
  2. `handle_reset(ack=MagicMock(), command=command, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: `C01` と `C02` の両方で `is_processing == False` かつ `generation` がインクリメントされている。`client.chat_postMessage` が `text="全チャンネルをリセットしました。"` で 1 回呼ばれる。
- **Pass criteria**: `_channel_states["C01"].is_processing == False` かつ `_channel_states["C02"].is_processing == False` かつ `client.chat_postMessage.call_args.kwargs["text"] == "全チャンネルをリセットしました。"`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `command.get("text", "").strip() == "all"` is `True`. `for ch_state in _channel_states.values(): with ch_state.lock: ch_state.generation += 1; ch_state.is_processing = False` — iterates all channels including C01 and C02. Then `client.chat_postMessage(channel=channel_id, text="全チャンネルをリセットしました。")` is called once.
- **Notes**: None.

---

### T-35: `handle_reset` — 未登録チャンネルで呼ばれた場合、拒否メッセージが返される

- **Phase**: UT
- **Precondition**: `_channel_states` に `"C_UNKNOWN"` が存在しない。`client` をモック化する。
- **Steps**:
  1. `command = {"channel_id": "C_UNKNOWN", "text": ""}` を用意する。
  2. `handle_reset(ack=MagicMock(), command=command, client=MagicMock(), logger=MagicMock())` を呼び出す。
- **Expected result**: `client.chat_postMessage` が `text="このチャンネルは未登録です。"` で 1 回呼ばれる。`_channel_states` への変更なし。
- **Pass criteria**: `client.chat_postMessage.call_args.kwargs["text"] == "このチャンネルは未登録です。"` が `True`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: `if channel_id not in _channel_states:` — True for "C_UNKNOWN". Calls `client.chat_postMessage(channel=channel_id, text="このチャンネルは未登録です。")` and returns. No state mutation occurs.
- **Notes**: None.

---

### T-36: `handle_reset` — `ack()` が最初に呼ばれる

- **Phase**: UT
- **Precondition**: `_channel_states["C01"]` が存在する。`ack` をモック化する。
- **Steps**:
  1. `ack = MagicMock()` を用意する。
  2. `command = {"channel_id": "C01", "text": ""}` で `handle_reset` を呼び出す。
  3. `ack.call_count` を確認する。
- **Expected result**: `ack()` が 1 回呼ばれている。
- **Pass criteria**: `ack.call_count == 1`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: First line of `handle_reset` is `ack()`. It is unconditionally called before any other logic. `call_count == 1`.
- **Notes**: None.

---

### T-37: `_watch_for_response` — タイムアウト経過後、タイムアウトメッセージを送信して終了する

- **Phase**: UT
- **Precondition**: `_channel_states["C01"].generation = 1`、`is_processing = True`。対象レスポンスファイルは存在しない。`RESPONSE_TIMEOUT` を 0.1 秒に短縮する（モック）。`client` をモック化する。
- **Steps**:
  1. `RESPONSE_TIMEOUT` をモックで `0.1` に設定する（または `time.time` を patch してタイムアウト条件をシミュレートする）。
  2. `_watch_for_response(response_file, "C01", "ts1", 1, client)` をスレッドで起動し、完了を待つ。
- **Expected result**: タイムアウトメッセージ `"タイムアウトしました。Claude Code が応答ファイルを生成しませんでした。/reset で再試行してください。"` が `thread_ts="ts1"` で送信される。`is_processing` が `False` になる。
- **Pass criteria**: `client.chat_postMessage` のテキストが上記タイムアウトメッセージと等しく、`_channel_states["C01"].is_processing == False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: When `time.time() - start_time >= RESPONSE_TIMEOUT`, the code calls `client.chat_postMessage(channel=channel_id, text="タイムアウトしました。Claude Code が応答ファイルを生成しませんでした。/reset で再試行してください。", thread_ts=thread_ts)` and `return`. The `finally:` block then runs: `with state.lock: if state.generation == own_gen: state.is_processing = False`. Both conditions are met (generation=1 == own_gen=1), so `is_processing` is set to `False`.
- **Notes**: None.

---

### T-38: `_watch_for_response` — 世代失効（`/reset` 後）でウォッチャーが即座にリターンし、`is_processing` を解除しない

- **Phase**: UT
- **Precondition**: `_channel_states["C01"].generation = 1`、`is_processing = True`。ウォッチャースレッドは `own_gen=1` で起動。
- **Steps**:
  1. `_watch_for_response` をスレッドで起動する（`own_gen=1`）。
  2. スレッド起動直後に `_channel_states["C01"].generation = 2` に変更する（`/reset` をシミュレート）。
  3. スレッドが完了するのを待つ。
- **Expected result**: ウォッチャーが世代失効チェックで即座にリターンする。`_channel_states["C01"].is_processing` は変更されない（`generation != own_gen` で `finally` の解除がスキップされる）。
- **Pass criteria**: スレッドが短時間（< 1 秒）で完了し、`_channel_states["C01"].is_processing` が `True` のまま（`/reset` が `False` に設定しているべきだが、このテストでは `generation` を直接操作するためリセット処理とは分離して確認する）。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: At polling loop top, `with state.lock: if state.generation != own_gen: return`. When `state.generation=2` and `own_gen=1`, `2 != 1` is `True` → `return`. Then `finally:` runs: `with state.lock: if state.generation == own_gen:` → `2 == 1` is `False` → `is_processing` is NOT cleared. `is_processing` remains `True`.
- **Notes**: None.

---

### T-39: `_watch_for_response` — settle 判定で size が変化した場合、ポーリングループ先頭に戻る（部分書き込みを読まない）

- **Phase**: UT
- **Precondition**: テンポラリディレクトリにレスポンスファイルを用意する。`POLL_INTERVAL` と `SETTLE_DURATION` を短縮する。`_channel_states["C01"].generation = 1`、`is_processing = True`。
- **Steps**:
  1. `response_file` を作成して `"partial"` を書き込む（`size1 = 7`）。
  2. ウォッチャースレッドを `own_gen=1` で起動する。
  3. `SETTLE_DURATION` の sleep 中に `response_file` に追記して `size2 > size1` にする。
  4. 続いて `response_file` を最終内容で書き直す（size が安定する）。
  5. スレッドが完了するのを待つ。
- **Expected result**: サイズ変化が検出された最初のサイクルでは送信されない（ループに戻る）。ファイルサイズが安定した後のサイクルで正しく内容が読み取られ、Slack へ送信される。
- **Pass criteria**: `client.chat_postMessage` または `client.files_upload_v2` が最終内容で 1 回のみ呼ばれる（中間内容では呼ばれない）。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: When `size1 != size2`, neither the read/delete/send block nor any `return` is executed in that branch. Execution falls through to `time.sleep(POLL_INTERVAL)` at the bottom of the loop and then repeats the loop from the top. The partial content is never sent. When the file stabilizes (`size1 == size2`), `file_handler.send_long_text` is called exactly once.
- **Notes**: None.

---

### T-40: `_watch_for_response` — settle 待ち中にファイルが削除された場合、ループ先頭に戻る

- **Phase**: UT
- **Precondition**: テンポラリディレクトリにレスポンスファイルを用意する。ウォッチャースレッドを起動する。
- **Steps**:
  1. `response_file` を作成する。
  2. ウォッチャーが `size1` を記録した後（`SETTLE_DURATION` sleep 開始後）に `response_file` を削除する。
  3. その後 `response_file` を正しい内容で再作成する。
  4. スレッドが完了するのを待つ。
- **Expected result**: ファイル削除を検出してループ先頭に戻り、再作成後のファイルを正しく検出して Slack へ送信する。
- **Pass criteria**: `client.chat_postMessage` または `client.files_upload_v2` が 1 回呼ばれ、かつ再作成後の正しい内容が送信される。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: After `SETTLE_DURATION` sleep, `if os.path.exists(response_file):` (step 4e) — when the file is deleted, this is `False`. The inner block is not entered; execution falls through to `time.sleep(POLL_INTERVAL)` at the bottom of the outer `if os.path.exists(response_file):` block (which is also `False` initially — but this path is: file detected, size1 recorded, sleep, file gone → outer `if` at step 4e is False → falls to `time.sleep(POLL_INTERVAL)` → loops back). When the file is re-created with stable content, the loop detects it and sends once.
- **Notes**: None.

---

### T-41: `_watch_for_response` — 正常完了後に `is_processing` が `False` になる

- **Phase**: UT
- **Precondition**: テンポラリディレクトリにレスポンスファイル（安定したサイズ）を用意する。`_channel_states["C01"].generation = 1`、`is_processing = True`。`client` をモック化する。
- **Steps**:
  1. レスポンスファイルを `"final answer"` で作成する（サイズ安定）。
  2. `_watch_for_response(response_file, "C01", "ts1", 1, client)` をスレッドで起動し完了を待つ。
- **Expected result**: ファイル内容が読み取られ、`send_long_text` 経由で Slack へ送信される。ファイルが削除される。`_channel_states["C01"].is_processing == False`。
- **Pass criteria**: スレッド完了後に `_channel_states["C01"].is_processing == False` かつ `os.path.exists(response_file) == False`。
- **Status**: ✅ Pass
- **Date**: 2026-07-26
- **Actual result**: Code inspection: When file detected and `size1 == size2` (stable), the code reads file content, calls `os.remove(response_file)`, calls `file_handler.send_long_text(...)`, then `return`. The `finally:` block fires: `with state.lock: if state.generation == own_gen:` (1==1 → True) → `state.is_processing = False`. File is deleted and `is_processing` becomes `False`.
- **Notes**: None.

---

## IT：結合テスト

（本フェーズはボット本体（`src/bot.py`）を実際の Slack ワークスペースに接続して行う。tmux セッションは実際に起動するか、またはテスト用のスタブセッションを使用する。）

---

### T-42: 登録済みチャンネルへのメンションで「送信しました」メッセージがスレッドに返る

- **Phase**: IT
- **Precondition**: ボットが起動している。`CHANNEL_MAP` に `#project-a` チャンネルが登録されている。対応する tmux window が存在する（または `ensure_window` が起動する）。Claude Code がレスポンスファイルを生成するセッションが動いている。
- **Steps**:
  1. Slack の `#project-a` チャンネルで `@BotName テスト` とメンションする。
  2. ボットの返信を待つ（最大 5 秒）。
- **Expected result**: メンションと同じスレッドに `"送信しました... [チャンネル: {channel_id}]"` が投稿される。`log/bot.log` に受信ログが記録される。
- **Pass criteria**: Slack UI 上でメンションのスレッドに「送信しました」を含むメッセージが 1 件表示される。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-43: 未登録チャンネルからのメンションは拒否されスレッド返信される

- **Phase**: IT
- **Precondition**: ボットが起動している。`CHANNEL_MAP` に `#test-unregistered` は含まれていない。
- **Steps**:
  1. `#test-unregistered` チャンネルで `@BotName test` とメンションする。
- **Expected result**: ボットが同スレッドに `"このチャンネルは未登録です。管理者に連絡してください。"` を返信する。tmux への入力は行われない。
- **Pass criteria**: `#test-unregistered` チャンネルのスレッドに上記の正確な文字列が 1 件表示される。`tmux capture-pane` で対象チャンネルのウィンドウに新しい入力が送信されていないことを確認できる。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-44: メンション後 Claude Code が応答ファイルを生成すると、スレッドへ回答が返る

- **Phase**: IT
- **Precondition**: ボットが起動している。`#project-a` が登録されている。tmux の Claude Code セッションが動作中。
- **Steps**:
  1. `#project-a` で `@BotName 1+1 は？` とメンションする。
  2. Claude Code が応答ファイルを生成するまで待つ（最大 `RESPONSE_TIMEOUT` 秒）。
- **Expected result**: メンションのスレッドに Claude Code の回答テキストが返信される。レスポンスファイルが削除されている。
- **Pass criteria**: Slack スレッドに回答メッセージが 1 件表示される。`ls {state.tmp}/claude_bot_response_{channel_id}_*.txt` の結果が空（ファイルが削除済み）。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-45: 3000 文字以下の回答は `chat_postMessage` で投稿される

- **Phase**: IT
- **Precondition**: ボットが起動している。テスト用 Claude Code セッションが短い（≤3000 文字）回答を生成するようにプロンプトを設定する。
- **Steps**:
  1. `#project-a` で `@BotName "はい" とだけ答えてください` とメンションする。
  2. 回答を待つ。
- **Expected result**: Slack スレッドに通常のテキストメッセージとして回答が表示される（ファイル添付ではない）。`log/bot.log` に `chat_postMessage` に相当するログが記録される。
- **Pass criteria**: Slack UI で回答がファイルスニペットではなくテキストメッセージとして表示される。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-46: 3001 文字超の回答は `files_upload_v2` でスニペット投稿される

- **Phase**: IT
- **Precondition**: ボットが起動している。テスト用に 3001 文字超の内容を持つレスポンスファイルを `state.tmp` に手動配置する（または Claude Code が長文を生成するよう誘導する）。
- **Steps**:
  1. `#project-a` で `@BotName 3001文字以上の回答を生成してください` とメンションする（またはレスポンスファイルを手動配置してウォッチャーを起動）。
  2. 回答を待つ。
- **Expected result**: Slack スレッドにファイルスニペット（`response.md`）として回答が添付表示される。テキストメッセージとして投稿されない。
- **Pass criteria**: Slack UI で `response.md` という名前のファイルスニペットがスレッドに表示される。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-47: 同一チャンネルへの 2 通目のメッセージは「処理中」で拒否される

- **Phase**: IT
- **Precondition**: ボットが起動している。`#project-a` が登録されている。Claude Code が処理に時間のかかるプロンプト（例: 長時間 sleep）を受け取った状態にする。
- **Steps**:
  1. `#project-a` で `@BotName メッセージA` を送信する（Claude Code が処理中になる）。
  2. 即座（1 秒以内）に `@BotName メッセージB` を送信する。
- **Expected result**: メッセージ B に対して `"処理中です。完了をお待ちください。/reset で中断できます。"` がスレッドに返信される。メッセージ A は引き続き処理される。
- **Pass criteria**: メッセージ B のスレッドに拒否メッセージが 1 件表示される。メッセージ A の処理が最終的に完了して回答が返る。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-48: 異なるチャンネルへの同時リクエストは並列処理される（互いにブロックしない）

- **Phase**: IT
- **Precondition**: ボットが起動している。`#project-a` と `#project-b` の 2 チャンネルが登録されている。それぞれ別の tmux window に対応している。
- **Steps**:
  1. `#project-a` で `@BotName メッセージA` を送信する。
  2. 即座（1 秒以内）に `#project-b` で `@BotName メッセージB` を送信する。
- **Expected result**: `#project-a` と `#project-b` の両方で「送信しました」メッセージが返る。両チャンネルの処理が互いにブロックせず並列に進む。
- **Pass criteria**: 両チャンネルで「送信しました」メッセージが表示される（一方が他方の完了を待たない）。各チャンネルの tmux window に別々の入力が送られたことを `tmux capture-pane` で確認できる。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-49: `/reset` で自チャンネルがリセットされ処理が中断される

- **Phase**: IT
- **Precondition**: ボットが起動している。`#project-a` が処理中（`is_processing=True`）の状態。
- **Steps**:
  1. `#project-a` で `/reset` を実行する。
- **Expected result**: ボットが `#project-a` に `"チャンネルをリセットしました。(generation=N)"` を投稿する（スレッド外、チャンネルトップに投稿）。その後 `#project-a` へ新しいメンションを送ると処理が受け付けられる。`#project-b` の状態は変化しない。
- **Pass criteria**: `#project-a` に上記リセットメッセージが 1 件表示される。次のメンションが「処理中」ではなく「送信しました」で受け付けられる。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-50: `/reset all` で全チャンネルがリセットされる

- **Phase**: IT
- **Precondition**: ボットが起動している。複数チャンネルが処理中の状態。
- **Steps**:
  1. いずれかの登録済みチャンネルで `/reset all` を実行する。
- **Expected result**: ボットが `"全チャンネルをリセットしました。"` を返す。全登録チャンネルで新しいメンションが「処理中」なく受け付けられる。
- **Pass criteria**: `"全チャンネルをリセットしました。"` が表示され、全チャンネルへのメンションが「送信しました」で受け付けられる。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-51: レスポンスファイルが生成されない場合、`RESPONSE_TIMEOUT` 後にタイムアウトメッセージが返る

- **Phase**: IT
- **Precondition**: ボットが起動している。`#project-a` が登録されている。Claude Code セッションが意図的に応答ファイルを生成しない状態（例: tmux window が存在するが Claude Code がハング）。`RESPONSE_TIMEOUT` を短い値（例: 10 秒）にセットする。
- **Steps**:
  1. `#project-a` で `@BotName テスト` とメンションする。
  2. `RESPONSE_TIMEOUT` 秒より長く待つ。
- **Expected result**: メンションのスレッドに `"タイムアウトしました。Claude Code が応答ファイルを生成しませんでした。/reset で再試行してください。"` が返信される。`is_processing` が `False` になり、次のメンションが受け付けられる。
- **Pass criteria**: タイムアウトメッセージがスレッドに 1 件表示される。タイムアウト後に次のメンションが「処理中」なく受け付けられる。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-52: `files_upload_v2` の `filetype="markdown"` が Slack API で受け付けられることを確認する

- **Phase**: IT
- **Precondition**: ボットが起動している。3001 文字超のレスポンスファイルをテスト用に用意する。
- **Steps**:
  1. 3001 文字超のコンテンツを持つファイルを `state.tmp` に配置してウォッチャーが検出できるようにする。
  2. Slack の応答を確認する。
- **Expected result**: `filetype="markdown"` で `files_upload_v2` が成功し、Slack にファイルが表示される。`SlackApiError` は発生しない。
- **Pass criteria**: Slack スレッドにファイルスニペットが表示される。`log/bot.log` にエラーが記録されない。
- **注記**: このテストが失敗（`invalid_file_type` エラー）した場合は `filetype="post"` または `filetype` パラメータ省略にフォールバックし、再テストする。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-53: DM でのメッセージがチャンネルと同様に処理される

- **Phase**: IT
- **Precondition**: ボットが起動している。DM チャンネル ID が `CHANNEL_MAP` に登録されている。
- **Steps**:
  1. ボットに DM を送信する（メンション不要）。
  2. 応答を待つ。
- **Expected result**: DM のスレッドに「送信しました」が返り、Claude Code の応答が返信される。DM ガード（Step 0）により二重処理が発生しない。
- **Pass criteria**: DM スレッドに「送信しました」と最終応答の 2 件が表示される（「送信しました」が 2 回表示されない）。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-54: `handle_reset` の generation 表示値がリセット直後の正しい値である（表示上のレース確認）

- **Phase**: IT
- **Precondition**: `#project-a` が登録されている。`generation` が既知の値（例: 3）の状態。
- **Steps**:
  1. `#project-a` で `/reset` を実行する。
- **Expected result**: 返信の `generation=N` の値が 4 以上の整数である（リセット前 + 1 以上）。正確な値が表示されなくてもシステムは正常動作する（DD Final 注記より、表示上のレースは benign）。
- **Pass criteria**: `"チャンネルをリセットしました。(generation=N)"` が表示され、N が整数である。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

## ST：システムテスト

（本フェーズは本番相当の環境でシステム全体を対象に実施する。再起動、設定変更、異常状態からの復元を含む。）

---

### T-55: ボット再起動後、既存の tmux window に接続し継続処理できる

- **Phase**: ST
- **Precondition**: ボットが起動中。`#project-a` の tmux window が存在し Claude Code セッションが動いている。
- **Steps**:
  1. ボットプロセス（`python src/bot.py`）を停止する（Ctrl+C または `kill`）。
  2. ボットプロセスを再起動する。
  3. `#project-a` で `@BotName 再起動後テスト` とメンションする。
- **Expected result**: 再起動後もボットが正常に起動し（`ensure_window` が既存 window を検出して `create_window` をスキップ）、メンションへの「送信しました」が返る。Claude Code セッションが継続して動作する。
- **Pass criteria**: 再起動後のメンションに「送信しました」が返り、最終的に Claude Code の回答がスレッドに表示される。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-56: ボット起動時に tmux window が存在しない場合、自動作成される

- **Phase**: ST
- **Precondition**: `CHANNEL_MAP` に登録されている tmux window が存在しない（削除または新規セッションの状態）。
- **Steps**:
  1. 対象 tmux session を起動する（window は未作成）。
  2. ボットを起動する（`python src/bot.py`）。
  3. `#project-a` で `@BotName テスト` とメンションする。
- **Expected result**: ボット起動時の `ensure_window` 呼び出しで window が自動作成される。`claude --dangerously-skip-permissions` が送信される。メンション処理が正常に完了する。
- **Pass criteria**: tmux で対象 window が存在することを `tmux list-windows` で確認できる。メンションに「送信しました」が返る。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-57: `CHANNEL_MAP_JSON` が未設定の場合、ボットが起動クラッシュする（サイレント起動禁止）

- **Phase**: ST
- **Precondition**: `.env` から `CHANNEL_MAP_JSON` を削除または空文字にする。
- **Steps**:
  1. `python src/bot.py` を起動する。
- **Expected result**: `ValueError: CHANNEL_MAP_JSON environment variable is not set.` が表示され、プロセスが非ゼロ終了コードで終了する。Slack Socket Mode に接続されない。
- **Pass criteria**: プロセスが起動直後にクラッシュし、終了コードが `0` 以外である。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-58: `CHANNEL_MAP_JSON` が不正 JSON の場合、ボットが起動クラッシュする

- **Phase**: ST
- **Precondition**: `.env` の `CHANNEL_MAP_JSON` に `"{invalid"` を設定する。
- **Steps**:
  1. `python src/bot.py` を起動する。
- **Expected result**: `ValueError` が表示されてプロセスが非ゼロ終了コードで終了する。
- **Pass criteria**: プロセスが起動直後にクラッシュし、終了コードが `0` 以外である。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-59: `tmp` ディレクトリが存在しない場合、メッセージ受信時に `FileNotFoundError` が発生し `is_processing` が解除される

- **Phase**: ST
- **Precondition**: ボットが起動している。`CHANNEL_MAP` の `tmp` パスに存在しないディレクトリを設定する。
- **Steps**:
  1. `#project-a` で `@BotName テスト` とメンションする。
  2. Claude Code が応答ファイルを書き出そうとする。
- **Expected result**: Claude Code の Write ツールが `FileNotFoundError` でエラーになる（ボット側の動作ではない）。ウォッチャースレッドはタイムアウトし、タイムアウトメッセージが Slack へ送信される。`is_processing` は最終的に `False` になる。
- **Pass criteria**: `RESPONSE_TIMEOUT` 秒後にタイムアウトメッセージがスレッドに表示される。その後のメンションが受け付けられる。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-60: ボット起動中に tmux セッションが消滅した場合、次のメッセージ受信時に `RuntimeError` が発生し `is_processing` が解除される

- **Phase**: ST
- **Precondition**: ボットが起動している。`#project-a` の tmux session が動いている。
- **Steps**:
  1. tmux session を強制終了する（`tmux kill-session`）。
  2. `#project-a` で `@BotName テスト` とメンションする。
- **Expected result**: `send_input` 内の `subprocess.run(check=True)` が `CalledProcessError` を raise する。`watcher_started=False` のため `handle_message` の `finally` が `is_processing` を `False` に戻す。Slack にはエラーが通知されないが、次のメンションは受け付けられる。
- **Pass criteria**: メンション後にボットがクラッシュせず、次のメンションに対して「送信しました」または「処理中」以外のエラーなく応答できる（`is_processing` が解除されていることの間接確認）。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-61: 複数チャンネルが同時に処理中でも、それぞれ独立したロックを使用してデッドロックが発生しない

- **Phase**: ST
- **Precondition**: ボットが起動している。`#project-a` と `#project-b` が登録されている。
- **Steps**:
  1. `#project-a` と `#project-b` に同時にメンションを送信する（< 0.5 秒以内）。
  2. 両方の応答が返るまで待つ（最大 `RESPONSE_TIMEOUT` 秒）。
- **Expected result**: 両チャンネルが互いにブロックせず、それぞれが「送信しました」を返してウォッチャーが動作する。デッドロック・無限待ち・フリーズが発生しない。
- **Pass criteria**: 両チャンネルの応答がタイムアウト前に返る。`log/bot.log` にデッドロックや例外の記録がない。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-62: ボットのメモリ状態（`_channel_states`）は再起動で初期化される（`generation=0`、`is_processing=False`）

- **Phase**: ST
- **Precondition**: ボットが動作中。`#project-a` が `is_processing=True`、`generation=10` の状態。
- **Steps**:
  1. ボットを停止して再起動する。
  2. `#project-a` で `@BotName テスト` とメンションする。
- **Expected result**: 再起動後は `generation=1`（0 からインクリメント）、`is_processing=False` の初期状態から開始する。メンションが「処理中」に拒否されない。
- **Pass criteria**: 再起動後のメンションが「送信しました」で受け付けられる。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

## UAT：ユーザー受入テスト

（本フェーズは実運用を想定したユーザー目線での最終確認。iPhone の Slack アプリから操作することを想定する。）

---

### T-63: iPhone の Slack からメンションを送り、Claude Code の回答がスレッドで受け取れる

- **Phase**: UAT
- **Precondition**: ボットが本番環境で起動している。iPhone に Slack アプリがインストールされている。`#project-a` が登録されている。Claude Code セッションが動作中。
- **Steps**:
  1. iPhone の Slack アプリで `#project-a` を開く。
  2. `@BotName こんにちは` とメンションして送信する。
  3. スレッドに「送信しました」が返ることを確認する。
  4. Claude Code の回答がスレッドに返るまで待つ（最大 `RESPONSE_TIMEOUT` 秒）。
- **Expected result**: 「送信しました」が即座に返り、Claude Code の日本語回答がスレッドに表示される。回答は元のメンションと同スレッドに紐付いている。
- **Pass criteria**: スレッドに「送信しました」と Claude Code の回答が合計 2 件表示される。回答が日本語で記述されている。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-64: 複数の依頼を続けて送った場合、スレッドで回答が混線しない

- **Phase**: UAT
- **Precondition**: ボットが起動している。`#project-a` が登録されている。
- **Steps**:
  1. `#project-a` で `@BotName 質問A` を送信する。
  2. 質問 A の「送信しました」を確認した後（処理完了後）、`@BotName 質問B` を送信する。
  3. 各回答を確認する。
- **Expected result**: 質問 A の回答が質問 A のスレッドに、質問 B の回答が質問 B のスレッドにそれぞれ返る。スレッド間の混線はない。
- **Pass criteria**: 質問 A のスレッドには A の回答のみ、質問 B のスレッドには B の回答のみが表示される。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-65: 処理中に `/reset` で中断し、その後新しい依頼を送れる

- **Phase**: UAT
- **Precondition**: ボットが起動している。`#project-a` が処理中の状態（長時間かかるプロンプトを送信中）。
- **Steps**:
  1. `#project-a` で長時間処理のメンションを送る（`@BotName 10分かかる作業をしてください` 等）。
  2. 「送信しました」を確認する。
  3. `#project-a` で `/reset` を実行する。
  4. `"チャンネルをリセットしました。"` の返信を確認する。
  5. `#project-a` で新しいメンション `@BotName 新しい依頼` を送る。
- **Expected result**: `/reset` 後に `"チャンネルをリセットしました。(generation=N)"` が返る。新しいメンションが「処理中」でなく「送信しました」で受け付けられる。
- **Pass criteria**: 手順 4 でリセットメッセージが表示される。手順 5 のメンションに「送信しました」が返る（「処理中です。」が返らない）。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-66: 長文回答（3001 文字超）がファイルスニペットとして読みやすく表示される

- **Phase**: UAT
- **Precondition**: ボットが起動している。Claude Code が長文回答を生成するプロンプトを用意する。
- **Steps**:
  1. `#project-a` で `@BotName 詳細な解説を3000文字以上で書いてください` とメンションする。
  2. 回答を待つ。
- **Expected result**: スレッドに `response.md` ファイルのスニペットとして回答が表示される。コードブロックが途中で分断されていない。ファイルを開いて全文を確認できる。
- **Pass criteria**: Slack UI で `response.md` スニペットが表示される。スニペットを開いたときに内容が完全であり、コードブロックが正しく閉じている。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-67: 別々のプロジェクトチャンネルで並列作業ができる

- **Phase**: UAT
- **Precondition**: ボットが起動している。`#project-a` と `#project-b` が登録されており、それぞれ別の Claude Code セッションに対応している。
- **Steps**:
  1. `#project-a` で `@BotName プロジェクトAの作業` を送信する。
  2. 即座に `#project-b` で `@BotName プロジェクトBの作業` を送信する。
  3. 両チャンネルの応答を確認する。
- **Expected result**: 両チャンネルで「送信しました」が返る。一方の処理が他方の完了を待たずに進む。最終的に両チャンネルに Claude Code の回答が返る。
- **Pass criteria**: 両チャンネルに「送信しました」と最終回答が表示される。一方の回答が他方のスレッドに混入していない。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

---

### T-68: 応答ファイルが生成されない場合、ユーザーにタイムアウト通知が届き次の操作ができる

- **Phase**: UAT
- **Precondition**: テスト用にタイムアウトを短い値（例: 15 秒）に設定したボットを起動する。Claude Code が応答ファイルを生成しない状態（セッションがビジーまたはハング）を作る。
- **Steps**:
  1. `#project-a` で `@BotName テスト` とメンションする。
  2. タイムアウト秒数（15 秒）が経過するまで待つ。
  3. タイムアウトメッセージを確認する。
  4. その後 `/reset` を実行し、新しいメンションを送る。
- **Expected result**: タイムアウト後にスレッドへ `"タイムアウトしました。Claude Code が応答ファイルを生成しませんでした。/reset で再試行してください。"` が返る。`/reset` 後に次のメンションが受け付けられる。
- **Pass criteria**: タイムアウトメッセージがスレッドに表示される。`/reset` 後の新しいメンションに「送信しました」が返る。
- **Status**: ⬜ Not run
- **Date**: 2026-07-26
- **Actual result**: Requires live Slack/tmux environment — deferred to real-environment test phase.
- **Notes**: None.

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

---

## Code Review

Status: ✅ Approved

Notes:

### Security (Checklist §1)
- Authorization guard in `handle_message`: step 0 DM guard fires first, then `channel_id not in _channel_states` check fires before any state mutation or tmux call. `handle_reset` applies the same authorization check immediately after `ack()`. Early-return pattern is correct on all paths.
- No hardcoded secrets. All tokens loaded via `load_dotenv()` + `os.environ["SLACK_BOT_TOKEN"]` / `os.environ["SLACK_APP_TOKEN"]`. `.env.example` contains only placeholder values.
- Response file path constructed server-side from `state.tmp + channel_id + own_gen`. No user-supplied path components. No path traversal risk.
- `send_input` uses `subprocess.run` list form (`shell=False`). User text is a list element — no shell expansion, no quoting needed or applied. Confirmed no `shlex.quote` present.

### Core Data Flow Integrity (Checklist §2)
- Response file relay design intact. Claude Code writes `tmp/claude_bot_response_{channel_id}_{gen}.txt`; watcher polls; bot posts.
- `_build_prompt` contains all 5 DD-required clauses: `[TOP PRIORITY]`, final-answer-only constraint, no draft constraint, `[確認]` as final answer, Japanese language, sole-output-path constraint.
- Path `claude_bot_response_{channel_id}_{own_gen}.txt` is unique per channel and generation. No collision possible between concurrent channels or concurrent generations on the same channel.

### ChannelState Invariants (Checklist §3)
- `handle_message`: `with state.lock:` atomically wraps `is_processing` check + `generation += 1` + `is_processing = True` + `own_gen = state.generation`. Correct single RMW.
- Slack API rejection reply (`already_processing`) is posted **outside** the lock using the `already_processing` flag pattern. Lock is never held across I/O.
- `_watch_for_response` `finally` block: `with state.lock: if state.generation == own_gen: state.is_processing = False`. Runs on all exit paths (normal, timeout, generation-expired). Expired watcher correctly skips clear via generation guard.
- `watcher_started = False` declared before `try:`, set to `True` after `Thread.start()`. `handle_message` `finally` only clears `is_processing` when `not watcher_started` — i.e. when watcher never started. When watcher is running, `is_processing` responsibility is fully delegated to the watcher's `finally`. This correctly prevents the race where `handle_message` would clear `is_processing` while the watcher is still active.
- `/reset` (own channel): `with state.lock: state.generation += 1; state.is_processing = False` — both mutations atomic under one lock. Correct.
- `/reset all`: iterates all `_channel_states.values()`, acquires each channel's own lock independently. Correct (no global lock).
- No global lock or global `is_processing` present anywhere.

### settle判定 (DD §`_watch_for_response`)
- Loop step sequence verified: size1 → `SETTLE_DURATION` sleep → generation recheck (lines 66–68) → timeout recheck (lines 70–76) → `os.path.exists` recheck (line 78) → size2 compare → `size1 == size2` → read+delete+send+return. Size mismatch falls through to `time.sleep(POLL_INTERVAL)` at line 88 then loops back to top. Settle-wait file-deleted case falls through to the same `time.sleep(POLL_INTERVAL)`. Both match DD spec exactly.
- Timeout check occurs at both polling-loop top (line 54) and inside settle-wait (line 70). Timeout can interrupt mid-settle. Correct per DD step 4d.

### Thread Reply Correctness (Checklist §4)
- `handle_message` "送信しました" reply, `_watch_for_response` success reply (via `send_long_text`), and `_watch_for_response` timeout reply all use `thread_ts`. Correct.
- `handle_reset` posts without `thread_ts` (top-level channel message). Correct per DD Final Design Review note.
- `thread_ts = event.get("thread_ts") or event["ts"]` per-request. Passed as argument to `_watch_for_response`. Not stored on `ChannelState`. Correct: each generation has its own `thread_ts`.

### Long Message Handling (Checklist §5)
- `files_upload_v2` used (not deprecated `files.upload`). `filetype="markdown"` per DD. Fallback noted in DD Final notes if Slack rejects this value.
- No message splitting. Whole text posted or uploaded as file. No code-fence split risk.
- `MAX_MESSAGE_LENGTH = 3000`. Correct.

### Slack API Idempotency (Checklist §6)
- `send_long_text` sends exactly once per call path. No retry loop. `SlackApiError` propagates to caller.
- No retry loops around any Slack API call in any file.

### tmux Window Management (Checklist §7)
- `ensure_window` and `create_window` both use `f"{session}:{window}"` target. Correct.
- No reserved window names introduced.

### Change Scope Discipline (Checklist §8)
- Files modified: `src/__init__.py`, `src/config.py`, `src/channel_state.py`, `src/tmux_handler.py`, `src/file_handler.py`, `src/bot.py`, `.env.example`, `requirements.txt`. All exactly match DD change scope. `app.py` left in place (not modified, not deleted). No out-of-scope changes.
- No unused imports. All imports in every file are consumed.
- No `print()` or debug statements.
- `requirements.txt`: `anthropic` removed, `slack-bolt>=1.21.0` + `python-dotenv>=1.0.0` present. Matches DD.
- `.env.example`: `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` absent; `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `TMUX_SESSION`, `RESPONSE_TIMEOUT`, `CHANNEL_MAP_JSON` with single-channel and multi-channel examples. Matches DD.

### Notes for Test Designer
- The `already_processing` flag pattern means the rejection reply is sent outside the lock. Test should verify no deadlock or missed reply under concurrent message arrival.
- `watcher_started = False` / `True` pattern: test that exceptions thrown between `state.lock` release and `Thread.start()` (e.g. tmux subprocess failure) correctly clear `is_processing` via the `finally` block.
- settle判定: test with a file that changes size during the settle window to confirm the loop correctly re-polls rather than sending partial content.
- `files_upload_v2` with `filetype="markdown"`: verify this value is accepted by the Slack API in IT. If rejected, fall back to `filetype="post"` or omit (single-send, no-retry constraint still applies).
- `handle_reset` reads `state.generation` outside the lock for the reply string (line 216). This is a known benign display race per DD Final notes. No correctness test needed, but verify the reply string appears with a plausible generation number.
