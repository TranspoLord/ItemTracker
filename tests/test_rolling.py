from pathlib import Path
import random
import tempfile
import unittest

from preyherbtracker.database import Database
from preyherbtracker.models import ItemCategory
from preyherbtracker.rolling import RollService


class RollingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.guild_id = 456
        self.database.create_clan(self.guild_id, "birchclan")
        self.database.add_item(self.guild_id, "tansy", ItemCategory.HERB.value)
        self.database.add_item(self.guild_id, "goldenrod", ItemCategory.HERB.value)
        self.service = RollService(self.database, rng=random.Random(7))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_roll_without_clan_does_not_store(self) -> None:
        result = self.service.forage(self.guild_id, category=ItemCategory.HERB.value, modifier=5)
        self.assertGreaterEqual(result.total, 1)
        self.assertFalse(result.stored)

    def test_roll_with_clan_stores_items(self) -> None:
        result = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=10,
            clan_name="birchclan",
        )
        storage = self.database.get_storage(self.guild_id, "birchclan", ItemCategory.HERB.value)
        self.assertTrue(result.finds)
        self.assertEqual(sum(storage.values()), len(result.finds))

    def test_dry_run_with_clan_does_not_store(self) -> None:
        result = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=10,
            clan_name="birchclan",
            store_results=False,
        )
        storage = self.database.get_storage(self.guild_id, "birchclan", ItemCategory.HERB.value)
        self.assertTrue(result.finds)
        self.assertFalse(result.stored)
        self.assertEqual(storage, {})

    def test_custom_roll_ranges_change_find_count(self) -> None:
        self.database.set_roll_ranges(
            self.guild_id,
            [
                (-100, 0, 0),
                (1, 30, 1),
            ],
        )
        result = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=10,
            clan_name="birchclan",
        )
        self.assertEqual(len(result.finds), 1)

    def test_required_items_need_territory_link(self) -> None:
        self.database.set_global_territory_requirement(self.guild_id, True)
        with self.assertRaises(ValueError):
            self.service.forage(
                self.guild_id,
                category=ItemCategory.HERB.value,
                modifier=5,
            )

        self.database.create_territory(self.guild_id, "ridge")
        self.database.set_territory_item_weight(
            self.guild_id,
            "ridge",
            ItemCategory.HERB.value,
            "tansy",
            5.0,
        )
        result = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=5,
            territory_name="ridge",
        )
        self.assertTrue(all(item == "tansy" for item in result.finds))

    def test_item_override_false_can_roll_anywhere(self) -> None:
        self.database.set_global_territory_requirement(self.guild_id, True)
        self.database.set_item_territory_requirement(
            self.guild_id,
            ItemCategory.HERB.value,
            "goldenrod",
            False,
        )

        result = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=10,
        )
        self.assertTrue(result.finds)
        self.assertTrue(all(item == "goldenrod" for item in result.finds))

    def test_category_stat_requirement_requires_stat_input(self) -> None:
        self.database.set_category_stat_requirement(self.guild_id, ItemCategory.HERB.value, True)

        with self.assertRaises(ValueError):
            self.service.forage(
                self.guild_id,
                category=ItemCategory.HERB.value,
                modifier=10,
            )

        result = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=10,
            stat=4,
        )
        self.assertTrue(result.finds)

    def test_required_stat_filters_items(self) -> None:
        self.database.update_item(self.guild_id, ItemCategory.HERB.value, "tansy", enabled=False)
        self.database.update_item(self.guild_id, ItemCategory.HERB.value, "goldenrod", enabled=False)
        self.database.add_item(self.guild_id, "mint", ItemCategory.HERB.value, required_stat=5)
        self.database.add_item(self.guild_id, "ginseng", ItemCategory.HERB.value, required_stat=12)

        low_stat = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=10,
            stat=6,
        )
        self.assertTrue(low_stat.finds)
        self.assertTrue(all(item == "mint" for item in low_stat.finds))

        high_stat = self.service.forage(
            self.guild_id,
            category=ItemCategory.HERB.value,
            modifier=10,
            stat=20,
        )
        self.assertTrue(high_stat.finds)
        self.assertTrue(all(item in {"mint", "ginseng"} for item in high_stat.finds))


if __name__ == "__main__":
    unittest.main()
