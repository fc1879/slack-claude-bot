import os
import re
import threading
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src import file_handler, tmux_handler
from src.channel_state import ChannelState, init_channel_states
from src.config import (
    CHANNEL_MAP,
    POLL_INTERVAL,
    RESPONSE_TIMEOUT,
    SETTLE_DURATION,
    TMUX_SESSION,
)

_channel_states: dict[str, ChannelState] = init_channel_states(CHANNEL_MAP)


def _build_prompt(message: str, response_file: str) -> str:
    return (
        f"{message}\n\n"
        f"[TOP PRIORITY] {response_file} = sole output channel. "
        "Write FINAL response via Write tool AFTER all reasoning/work — no drafts, no partial writes. "
        "([確認]フォーマット等の確認文も最終回答として書き出すこと。日本語で回答すること。)"
    )


def _watch_for_response(
    response_file: str,
    channel_id: str,
    thread_ts: str,
    own_gen: int,
    client,
) -> None:
    # _channel_states is read-only after initialization, so no lock needed for dict lookup.
    state = _channel_states[channel_id]
    start_time = time.time()

    try:
        while True:
            with state.lock:
                if state.generation != own_gen:
                    return

            if time.time() - start_time >= RESPONSE_TIMEOUT:
                client.chat_postMessage(
                    channel=channel_id,
                    text="タイムアウトしました。Claude Code が応答ファイルを生成しませんでした。/reset で再試行してください。",
                    thread_ts=thread_ts,
                )
                return

            if os.path.exists(response_file):
                size1 = os.path.getsize(response_file)
                time.sleep(SETTLE_DURATION)

                with state.lock:
                    if state.generation != own_gen:
                        return

                if time.time() - start_time >= RESPONSE_TIMEOUT:
                    client.chat_postMessage(
                        channel=channel_id,
                        text="タイムアウトしました。Claude Code が応答ファイルを生成しませんでした。/reset で再試行してください。",
                        thread_ts=thread_ts,
                    )
                    return

                if os.path.exists(response_file):
                    size2 = os.path.getsize(response_file)
                    if size1 == size2:
                        with open(response_file, "r", encoding="utf-8") as f:
                            content = f.read()
                        os.remove(response_file)
                        file_handler.send_long_text(client, channel_id, content, thread_ts)
                        return
                    # Size changed — file still being written; loop back with a poll pause.

            time.sleep(POLL_INTERVAL)
    finally:
        # Clear is_processing only if this watcher still owns the current generation.
        # A /reset or newer request may have already incremented generation.
        with state.lock:
            if state.generation == own_gen:
                state.is_processing = False


def handle_message(event, client, logger) -> None:
    # Step 0: prevent double-firing. Channel messages emit both app_mention and message events.
    # DMs have channel_type == "im" and must pass through.
    if event.get("type") == "message" and event.get("channel_type") != "im":
        return

    channel_id = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]

    if channel_id not in _channel_states:
        client.chat_postMessage(
            channel=channel_id,
            text="このチャンネルは未登録です。管理者に連絡してください。",
            thread_ts=thread_ts,
        )
        return

    text = event.get("text", "")
    clean_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

    if not clean_text:
        client.chat_postMessage(
            channel=channel_id,
            text="メッセージが空です。",
            thread_ts=thread_ts,
        )
        return

    state = _channel_states[channel_id]

    already_processing = False
    with state.lock:
        if state.is_processing:
            already_processing = True
        else:
            state.generation += 1
            state.is_processing = True
            own_gen = state.generation

    # Slack API call made after releasing the lock to avoid holding a lock across I/O.
    if already_processing:
        client.chat_postMessage(
            channel=channel_id,
            text="処理中です。完了をお待ちください。/reset で中断できます。",
            thread_ts=thread_ts,
        )
        return

    watcher_started = False
    try:
        response_file = os.path.join(
            state.tmp,
            f"claude_bot_response_{channel_id}_{own_gen}.txt",
        )

        prev_file = os.path.join(
            state.tmp,
            f"claude_bot_response_{channel_id}_{own_gen - 1}.txt",
        )
        if os.path.exists(prev_file):
            os.remove(prev_file)

        window = state.target.split(":")[-1]
        tmux_handler.ensure_window(TMUX_SESSION, window, state.cwd)

        full_prompt = _build_prompt(clean_text, response_file)
        tmux_handler.send_input(TMUX_SESSION, full_prompt, window=window)

        client.chat_postMessage(
            channel=channel_id,
            text=f"送信しました... [チャンネル: {channel_id}]",
            thread_ts=thread_ts,
        )

        threading.Thread(
            target=_watch_for_response,
            args=(response_file, channel_id, thread_ts, own_gen, client),
            daemon=True,
        ).start()
        watcher_started = True
    finally:
        # Only clear is_processing if the watcher never started.
        # When the watcher is running, its own finally block owns is_processing.
        if not watcher_started:
            with state.lock:
                if state.generation == own_gen:
                    state.is_processing = False


def handle_reset(ack, command, client, logger) -> None:
    ack()

    channel_id = command["channel_id"]

    if channel_id not in _channel_states:
        client.chat_postMessage(
            channel=channel_id,
            text="このチャンネルは未登録です。",
        )
        return

    if command.get("text", "").strip() == "all":
        for ch_state in _channel_states.values():
            with ch_state.lock:
                ch_state.generation += 1
                ch_state.is_processing = False
        client.chat_postMessage(
            channel=channel_id,
            text="全チャンネルをリセットしました。",
        )
        return

    state = _channel_states[channel_id]
    with state.lock:
        state.generation += 1
        state.is_processing = False

    client.chat_postMessage(
        channel=channel_id,
        text=f"チャンネルをリセットしました。(generation={state.generation})",
    )


def main() -> None:
    load_dotenv()

    app = App(token=os.environ["SLACK_BOT_TOKEN"])

    app.event("app_mention")(handle_message)
    app.event("message")(handle_message)
    app.command("/reset")(handle_reset)

    for channel_id, state in _channel_states.items():
        window = state.target.split(":")[-1]
        tmux_handler.ensure_window(TMUX_SESSION, window, state.cwd)

    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
