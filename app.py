import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])
anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-8")

# Keep per-user conversation history in memory
conversation_history: dict[str, list] = {}


def get_claude_response(user_id: str, user_message: str) -> str:
    history = conversation_history.setdefault(user_id, [])
    history.append({"role": "user", "content": user_message})

    response = anthropic.messages.create(
        model=MODEL,
        max_tokens=4096,
        system="You are a helpful assistant integrated into Slack. Be concise and clear.",
        messages=history,
    )

    assistant_message = response.content[0].text
    history.append({"role": "assistant", "content": assistant_message})

    return assistant_message


@app.event("app_mention")
def handle_mention(event, say):
    user_id = event["user"]
    text = event.get("text", "")

    # Remove the bot mention from the message
    import re
    clean_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    if not clean_text:
        say("何かご質問はありますか？")
        return

    response = get_claude_response(user_id, clean_text)
    say(f"<@{user_id}> {response}")


@app.message("!reset")
def handle_reset(message, say):
    user_id = message["user"]
    conversation_history.pop(user_id, None)
    say(f"<@{user_id}> 会話履歴をリセットしました。")


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("⚡ Slack Claude Bot is running!")
    handler.start()
