import os
import sys
from dataclasses import dataclass

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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

CHANNEL_MAP_FILE: str = os.environ.get(
    "CHANNEL_MAP_FILE",
    os.path.join(_PROJECT_ROOT, "channel_map.toml"),
)


def _load_channel_map() -> dict[str, ChannelConfig]:
    if not os.path.exists(CHANNEL_MAP_FILE):
        raise FileNotFoundError(
            f"channel_map.toml not found: {CHANNEL_MAP_FILE}\n"
            "Copy channel_map.example.toml to channel_map.toml and fill in your values."
        )
    try:
        with open(CHANNEL_MAP_FILE, "rb") as f:
            parsed = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{CHANNEL_MAP_FILE} is not valid TOML: {e}") from e

    channels = parsed.get("channels", {})
    result: dict[str, ChannelConfig] = {}
    for channel_id, cfg in channels.items():
        try:
            result[channel_id] = ChannelConfig(
                target=cfg["target"],
                cwd=cfg["cwd"],
                tmp=cfg["tmp"],
            )
        except KeyError as e:
            raise ValueError(
                f"channel_map.toml entry for '{channel_id}' is missing key {e}"
            ) from e
    return result


CHANNEL_MAP: dict[str, ChannelConfig] = _load_channel_map()
