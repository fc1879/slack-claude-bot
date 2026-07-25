import json
import os
from dataclasses import dataclass


@dataclass
class ChannelConfig:
    target: str
    cwd: str
    tmp: str


TMUX_SESSION: str = os.environ.get("TMUX_SESSION", "claude_session")
RESPONSE_TIMEOUT: int = int(os.environ.get("RESPONSE_TIMEOUT", "120"))
POLL_INTERVAL: float = 2.0
SETTLE_DURATION: float = 1.0
MAX_MESSAGE_LENGTH: int = 3000


def _load_channel_map() -> dict[str, ChannelConfig]:
    raw = os.environ.get("CHANNEL_MAP_JSON", "")
    if not raw:
        raise ValueError(
            "CHANNEL_MAP_JSON environment variable is not set. "
            "Set it to a JSON object mapping channel IDs to target/cwd/tmp."
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"CHANNEL_MAP_JSON is not valid JSON: {e}") from e

    result: dict[str, ChannelConfig] = {}
    for channel_id, cfg in parsed.items():
        try:
            result[channel_id] = ChannelConfig(
                target=cfg["target"],
                cwd=cfg["cwd"],
                tmp=cfg["tmp"],
            )
        except KeyError as e:
            raise ValueError(
                f"CHANNEL_MAP_JSON entry for '{channel_id}' is missing key {e}"
            ) from e
    return result


CHANNEL_MAP: dict[str, ChannelConfig] = _load_channel_map()
