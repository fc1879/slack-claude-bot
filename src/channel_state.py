import threading
from dataclasses import dataclass, field

from src.config import ChannelConfig


@dataclass
class ChannelState:
    target: str
    cwd: str
    tmp: str
    generation: int = 0
    is_processing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def init_channel_states(channel_map: dict[str, ChannelConfig]) -> dict[str, ChannelState]:
    return {
        channel_id: ChannelState(
            target=cfg.target,
            cwd=cfg.cwd,
            tmp=cfg.tmp,
        )
        for channel_id, cfg in channel_map.items()
    }
