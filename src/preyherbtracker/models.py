from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StringEnum(str, Enum):
    pass


class TrackingMode(StringEnum):
    OFF = "off"
    MANUAL = "manual"
    FORUM = "forum"
    SPREADSHEET = "spreadsheet"


class ItemCategory(StringEnum):
    HERB = "herb"
    PREY = "prey"


class AccessLevel(StringEnum):
    ADMIN = "admin"
    MOD = "mod"
    USER = "user"


@dataclass(slots=True)
class CategoryThreshold:
    mode: str  # 'static' (fixed item count) or 'dynamic' (per-cat multiplier)
    value: float


@dataclass(slots=True)
class Clan:
    id: int
    guild_id: int
    name: str
    tracking_mode: str
    tracking_link: str | None
    cat_count: int
    alert_channel_id: int | None
    alerts_enabled: bool
    seasonal_modifier: float = 1.0
    roll_log_channel_id: int | None = None


@dataclass(slots=True)
class Item:
    id: int
    guild_id: int
    name: str
    category: str
    enabled: bool
    is_default: bool
    required_stat: int | None = None
    required_stat_name: str | None = None


@dataclass(slots=True)
class TerritoryItem:
    item_name: str
    weight: float
    seasonal_modifier: float = 1.0

    @property
    def effective_weight(self) -> float:
        return self.weight * self.seasonal_modifier


DEFAULT_HERBS = [
    ("tansy", 1.0),
    ("goldenrod", 1.0),
    ("cobweb", 1.0),
    ("marigold", 1.0),
    ("catmint", 1.0),
]

DEFAULT_PREY = [
    ("mouse", 1.0),
    ("vole", 1.0),
    ("shrew", 1.0),
    ("rabbit", 2.0),
    ("squirrel", 2.0),
]
