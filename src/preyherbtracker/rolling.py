from __future__ import annotations

from dataclasses import dataclass
import random

from preyherbtracker.database import Database


@dataclass(slots=True)
class RollResult:
    die_roll: int
    modifier: int
    total: int
    finds: list[str]
    stored: bool
    clan_name: str | None
    territory_name: str | None
    category: str


class RollService:
    def __init__(self, database: Database, *, rng: random.Random | None = None) -> None:
        self.database = database
        self.rng = rng or random.Random()

    def forage(
        self,
        guild_id: int,
        *,
        category: str,
        modifier: int = 0,
        clan_name: str | None = None,
        territory_name: str | None = None,
        stat: int | None = None,
        store_results: bool = True,
        actor_user_id: int | None = None,
        actor_access_level: str | None = None,
    ) -> RollResult:
        die_roll = self.rng.randint(1, 20)
        total = die_roll + modifier
        find_count = self.database.get_find_count_for_total(guild_id, total)
        pool = self._load_pool(guild_id, category, territory_name, stat=stat)
        if not pool[0]:
            raise ValueError(f"No enabled {category} items are configured")
        finds = [self.rng.choices(pool[0], weights=pool[1], k=1)[0] for _ in range(find_count)]
        stored = False
        if store_results and clan_name and finds:
            for item_name in finds:
                self.database.adjust_storage(
                    guild_id,
                    clan_name,
                    category,
                    item_name,
                    1,
                    source="roll_forage",
                    metadata={
                        "die_roll": die_roll,
                        "modifier": modifier,
                        "territory": territory_name,
                    },
                    user_id=actor_user_id,
                    access_level=actor_access_level,
                )
            stored = True
        return RollResult(
            die_roll=die_roll,
            modifier=modifier,
            total=total,
            finds=finds,
            stored=stored,
            clan_name=clan_name,
            territory_name=territory_name,
            category=category,
        )

    def _load_pool(
        self,
        guild_id: int,
        category: str,
        territory_name: str | None,
        *,
        stat: int | None,
    ) -> tuple[list[str], list[float]]:
        items = self.database.list_items(guild_id, category, enabled_only=True)
        requirements = self.database.get_effective_item_territory_requirements(guild_id, category)
        stat_input_requirements = self.database.get_effective_item_stat_input_requirements(guild_id, category)

        def passes_stat(item_name: str, required_stat: int | None) -> bool:
            requires_stat_input = stat_input_requirements.get(item_name, False)
            if (requires_stat_input or required_stat is not None) and stat is None:
                return False
            if required_stat is not None and stat is not None and stat < required_stat:
                return False
            return True

        if territory_name:
            territory_items = self.database.list_territory_items(guild_id, territory_name, category)
            territory_map = {entry.item_name: entry for entry in territory_items}
            clan_seasonal, global_seasonal = self.database.get_seasonal_modifiers_for_territory(guild_id, territory_name)

            names: list[str] = []
            weights: list[float] = []
            for item in items:
                if not passes_stat(item.name, item.required_stat):
                    continue
                linked_entry = territory_map.get(item.name)
                if linked_entry is not None:
                    names.append(item.name)
                    weights.append(linked_entry.weight * linked_entry.seasonal_modifier * clan_seasonal * global_seasonal)
                    continue
                if not requirements.get(item.name, False):
                    names.append(item.name)
                    weights.append(1.0)
            return names, weights

        names = [
            item.name
            for item in items
            if not requirements.get(item.name, False) and passes_stat(item.name, item.required_stat)
        ]
        return names, [1.0 for _ in names]
