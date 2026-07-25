from src.config import MAX_MESSAGE_LENGTH


def send_long_text(client, channel: str, text: str, thread_ts: str) -> None:
    # chat_postMessage and files_upload_v2 are NOT idempotent.
    # Each call sends exactly once; SlackApiError propagates to the caller without retry.
    if len(text) <= MAX_MESSAGE_LENGTH:
        client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )
    else:
        client.files_upload_v2(
            channel=channel,
            content=text,
            filename="response.md",
            filetype="markdown",
            thread_ts=thread_ts,
        )
