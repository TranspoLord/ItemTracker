from pathlib import Path
import tempfile
import unittest

from preyherbtracker.database import COMMAND_ACCESS, Database
from preyherbtracker.models import ItemCategory


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.guild_id = 123
        self.database.create_clan(self.guild_id, "birchclan")
        self.database.add_item(self.guild_id, "tansy", ItemCategory.HERB.value)
        self.database.add_item(self.guild_id, "mouse", ItemCategory.PREY.value)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_adjust_storage_tracks_totals(self) -> None:
        quantity = self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            3,
            source="test",
        )
        self.assertEqual(quantity, 3)
        storage = self.database.get_storage(self.guild_id, "birchclan", ItemCategory.HERB.value)
        self.assertEqual(storage["tansy"], 3)

    def test_category_totals(self) -> None:
        self.database.adjust_storage(self.guild_id, "birchclan", ItemCategory.HERB.value, "tansy", 2, source="test")
        self.database.adjust_storage(self.guild_id, "birchclan", ItemCategory.PREY.value, "mouse", 5, source="test")
        totals = self.database.get_category_totals(self.guild_id, "birchclan")
        self.assertEqual(totals[ItemCategory.HERB.value], 2)
        self.assertEqual(totals[ItemCategory.PREY.value], 5)

    def test_command_access_overrides(self) -> None:
        command_name = "storage_add"
        default_level = COMMAND_ACCESS[command_name]

        self.assertEqual(
            self.database.get_command_access_level(self.guild_id, command_name, COMMAND_ACCESS),
            default_level,
        )

        self.database.set_command_access_override(self.guild_id, command_name, "user")
        overrides = self.database.get_command_access_overrides(self.guild_id)
        self.assertEqual(overrides[command_name], "user")
        self.assertEqual(
            self.database.get_command_access_level(self.guild_id, command_name, COMMAND_ACCESS),
            "user",
        )

        removed = self.database.clear_command_access_override(self.guild_id, command_name)
        self.assertTrue(removed)
        self.assertEqual(
            self.database.get_command_access_level(self.guild_id, command_name, COMMAND_ACCESS),
            default_level,
        )

    def test_user_permission_round_trip(self) -> None:
        user_id = 4567

        self.assertIsNone(self.database.get_user_permission(self.guild_id, user_id))

        self.database.create_access_level(self.guild_id, "healer", 100)
        self.database.set_user_permission(self.guild_id, "healer", user_id)
        self.assertEqual(self.database.get_user_permission(self.guild_id, user_id), "healer")

        # Re-setting the same user should update the mapped access level.
        self.database.set_user_permission(self.guild_id, "mod", user_id)
        self.assertEqual(self.database.get_user_permission(self.guild_id, user_id), "mod")

        user_map = self.database.get_user_permissions(self.guild_id)
        self.assertEqual(user_map["mod"], [user_id])

    def test_access_impersonation_round_trip(self) -> None:
        user_id = 456

        self.assertIsNone(self.database.get_access_impersonation(self.guild_id, user_id))

        self.database.set_access_impersonation(self.guild_id, user_id, "user")
        self.assertEqual(
            self.database.get_access_impersonation(self.guild_id, user_id),
            "user",
        )

        self.database.create_access_level(self.guild_id, "healer", 100)
        self.database.set_access_impersonation(self.guild_id, user_id, "healer")
        self.assertEqual(
            self.database.get_access_impersonation(self.guild_id, user_id),
            "healer",
        )

        removed = self.database.clear_access_impersonation(self.guild_id, user_id)
        self.assertTrue(removed)
        self.assertIsNone(self.database.get_access_impersonation(self.guild_id, user_id))

    def test_command_access_name_migration(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO command_access_overrides (guild_id, command_name, access_level) VALUES (?, ?, ?)",
                (self.guild_id, "seed_demo", "mod"),
            )

        self.database.initialize()

        overrides = self.database.get_command_access_overrides(self.guild_id)
        self.assertNotIn("seed_demo", overrides)
        self.assertEqual(overrides["test_seed_demo"], "mod")

    def test_clan_member_round_trip(self) -> None:
        user_id = 789

        self.assertFalse(self.database.user_has_clan_membership(self.guild_id, user_id, "birchclan"))

        self.database.add_clan_member(self.guild_id, "birchclan", user_id)
        self.assertTrue(self.database.user_has_clan_membership(self.guild_id, user_id, "birchclan"))
        self.assertEqual(self.database.list_clan_members(self.guild_id, "birchclan"), [user_id])
        self.assertEqual(self.database.list_member_clans(self.guild_id, user_id), ["birchclan"])

        removed = self.database.remove_clan_member(self.guild_id, "birchclan", user_id)
        self.assertTrue(removed)
        self.assertFalse(self.database.user_has_clan_membership(self.guild_id, user_id, "birchclan"))

    def test_audit_entries_respect_access_levels(self) -> None:
        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            2,
            source="manual_storage_add",
            user_id=11,
            access_level="user",
        )
        self.database.log_command_event(
            self.guild_id,
            "command_access_set",
            user_id=12,
            access_level="admin",
            details={"command_name": "storage_add"},
        )
        self.database.log_command_event(
            self.guild_id,
            "storage_show",
            user_id=13,
            access_level="user",
            clan_name="birchclan",
        )

        user_entries = self.database.get_audit_entries(
            self.guild_id,
            viewer_access_level="user",
            visible_clans=["birchclan"],
            limit=10,
        )
        self.assertEqual(len(user_entries), 1)
        self.assertEqual(user_entries[0]["kind"], "storage")

        mod_entries = self.database.get_audit_entries(
            self.guild_id,
            viewer_access_level="mod",
            limit=10,
        )
        self.assertEqual(len(mod_entries), 2)
        self.assertTrue(all(entry["access_level"] != "admin" for entry in mod_entries))

        admin_entries = self.database.get_audit_entries(
            self.guild_id,
            viewer_access_level="admin",
            limit=10,
        )
        self.assertEqual(len(admin_entries), 3)

    def test_user_undo_only_reverts_own_latest_storage_entry(self) -> None:
        user_id = 41
        self.database.add_clan_member(self.guild_id, "birchclan", user_id)
        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            5,
            source="manual_storage_add",
            user_id=90,
            access_level="mod",
        )
        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            2,
            source="manual_storage_add",
            user_id=user_id,
            access_level="user",
        )

        undone = self.database.undo_latest_storage_entry(
            self.guild_id,
            viewer_access_level="user",
            viewer_user_id=user_id,
            clan_name="birchclan",
            visible_clans=["birchclan"],
            actor_user_id=user_id,
            actor_access_level="user",
        )

        self.assertIsNotNone(undone)
        assert undone is not None
        self.assertEqual(undone["delta"], 2)
        storage = self.database.get_storage(self.guild_id, "birchclan", ItemCategory.HERB.value)
        self.assertEqual(storage["tansy"], 5)

    def test_admin_undo_reverts_latest_storage_entry(self) -> None:
        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            1,
            source="manual_storage_add",
            user_id=11,
            access_level="user",
        )
        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            4,
            source="manual_storage_add",
            user_id=12,
            access_level="mod",
        )

        undone = self.database.undo_latest_storage_entry(
            self.guild_id,
            viewer_access_level="admin",
            viewer_user_id=999,
            clan_name="birchclan",
            actor_user_id=999,
            actor_access_level="admin",
        )

        self.assertIsNotNone(undone)
        assert undone is not None
        self.assertEqual(undone["delta"], 4)
        storage = self.database.get_storage(self.guild_id, "birchclan", ItemCategory.HERB.value)
        self.assertEqual(storage["tansy"], 1)

    def test_territory_channel_link_round_trip(self) -> None:
        self.database.create_territory(self.guild_id, "marsh", channel_id=123456789012345678)

        names = self.database.list_territory_names(self.guild_id)
        self.assertIn("marsh", names)
        self.assertEqual(
            self.database.get_territory_channel_link(self.guild_id, "marsh"),
            123456789012345678,
        )

        self.database.set_territory_channel_link(self.guild_id, "marsh", 234567890123456789)
        self.assertEqual(
            self.database.get_territory_channel_link(self.guild_id, "marsh"),
            234567890123456789,
        )

        self.database.set_territory_channel_link(self.guild_id, "marsh", None)
        self.assertIsNone(self.database.get_territory_channel_link(self.guild_id, "marsh"))

    def test_detect_territory_from_channels_prefers_link_then_name(self) -> None:
        self.database.create_territory(self.guild_id, "ridge", channel_id=999)
        self.database.create_territory(self.guild_id, "valley")

        by_link = self.database.detect_territory_from_channels(
            self.guild_id,
            channel_ids=[999, 111],
            channel_names=["not-used"],
        )
        self.assertEqual(by_link, "ridge")

        by_name = self.database.detect_territory_from_channels(
            self.guild_id,
            channel_ids=[],
            channel_names=["Valley", "other"],
        )
        self.assertEqual(by_name, "valley")

        strict_none = self.database.detect_territory_from_channels(
            self.guild_id,
            channel_ids=[],
            channel_names=["Valley"],
            allow_name_fallback=False,
        )
        self.assertIsNone(strict_none)

    def test_territory_item_seasonal_modifier_persists(self) -> None:
        self.database.create_territory(self.guild_id, "ridge")
        self.database.set_territory_item_weight(
            self.guild_id,
            "ridge",
            ItemCategory.HERB.value,
            "tansy",
            3.0,
            seasonal_modifier=1.5,
        )
        entries = self.database.list_territory_items(self.guild_id, "ridge", ItemCategory.HERB.value)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].item_name, "tansy")
        self.assertEqual(entries[0].weight, 3.0)
        self.assertEqual(entries[0].seasonal_modifier, 1.5)
        self.assertEqual(entries[0].effective_weight, 4.5)

    def test_territory_requirement_hierarchy_and_force(self) -> None:
        self.database.add_item(self.guild_id, "rabbit", ItemCategory.PREY.value)

        # Default is global false, so no prey item requires territory links.
        initial = self.database.get_effective_item_territory_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertFalse(initial["mouse"])
        self.assertFalse(initial["rabbit"])

        # Item override survives global update when force is false.
        self.database.set_item_territory_requirement(self.guild_id, ItemCategory.PREY.value, "mouse", True)
        self.database.set_global_territory_requirement(self.guild_id, False, force=False)
        after_non_force = self.database.get_effective_item_territory_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertTrue(after_non_force["mouse"])
        self.assertFalse(after_non_force["rabbit"])

        # Force global update propagates to every item override.
        self.database.set_global_territory_requirement(self.guild_id, False, force=True)
        after_global_force = self.database.get_effective_item_territory_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertFalse(after_global_force["mouse"])
        self.assertFalse(after_global_force["rabbit"])

        # Category force update propagates down within that category.
        self.database.set_item_territory_requirement(self.guild_id, ItemCategory.PREY.value, "rabbit", False)
        self.database.set_category_territory_requirement(self.guild_id, ItemCategory.PREY.value, True, force=True)
        after_category_force = self.database.get_effective_item_territory_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertTrue(after_category_force["mouse"])
        self.assertTrue(after_category_force["rabbit"])

    def test_stat_requirement_hierarchy_and_force(self) -> None:
        self.database.add_item(self.guild_id, "rabbit", ItemCategory.PREY.value)

        initial = self.database.get_effective_item_stat_input_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertFalse(initial["mouse"])
        self.assertFalse(initial["rabbit"])

        self.database.set_item_stat_requirement(self.guild_id, ItemCategory.PREY.value, "mouse", True)
        self.database.set_global_stat_requirement(self.guild_id, False, force=False)
        after_non_force = self.database.get_effective_item_stat_input_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertTrue(after_non_force["mouse"])
        self.assertFalse(after_non_force["rabbit"])

        self.database.set_global_stat_requirement(self.guild_id, False, force=True)
        after_global_force = self.database.get_effective_item_stat_input_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertFalse(after_global_force["mouse"])
        self.assertFalse(after_global_force["rabbit"])

        self.database.set_item_stat_requirement(self.guild_id, ItemCategory.PREY.value, "rabbit", False)
        self.database.set_category_stat_requirement(self.guild_id, ItemCategory.PREY.value, True, force=True)
        after_category_force = self.database.get_effective_item_stat_input_requirements(self.guild_id, ItemCategory.PREY.value)
        self.assertTrue(after_category_force["mouse"])
        self.assertTrue(after_category_force["rabbit"])

    def test_export_audit_entries_supports_clan_filter(self) -> None:
        self.database.create_clan(self.guild_id, "oakclan")
        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            2,
            source="manual_storage_add",
            metadata={"roll_message_link": "https://discord.com/channels/1/2/3"},
            user_id=11,
            access_level="user",
        )
        self.database.adjust_storage(
            self.guild_id,
            "oakclan",
            ItemCategory.HERB.value,
            "tansy",
            1,
            source="manual_storage_add",
            user_id=12,
            access_level="mod",
        )
        self.database.log_command_event(
            self.guild_id,
            "storage_add",
            user_id=11,
            access_level="user",
            clan_name="birchclan",
            details={"command_link": "https://discord.com/channels/1/2/3"},
        )
        self.database.log_command_event(
            self.guild_id,
            "storage_add",
            user_id=12,
            access_level="mod",
            clan_name="oakclan",
        )

        exported = self.database.export_audit_entries(self.guild_id, since_days=31, clan_name="birchclan")

        self.assertEqual(exported["clan_name"], "birchclan")
        self.assertEqual(len(exported["storage_entries"]), 1)
        self.assertEqual(exported["storage_entries"][0]["clan_name"], "birchclan")
        self.assertEqual(exported["storage_entries"][0]["metadata"]["roll_message_link"], "https://discord.com/channels/1/2/3")
        self.assertEqual(len(exported["command_entries"]), 1)
        self.assertEqual(exported["command_entries"][0]["clan_name"], "birchclan")

    def test_clear_audit_entries_filters_by_clan_category_and_territory(self) -> None:
        self.database.create_clan(self.guild_id, "oakclan")

        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            1,
            source="roll_forage",
            metadata={"territory": "ridge"},
            user_id=1,
            access_level="user",
        )
        self.database.adjust_storage(
            self.guild_id,
            "birchclan",
            ItemCategory.HERB.value,
            "tansy",
            1,
            source="roll_forage",
            metadata={"territory": "bog"},
            user_id=1,
            access_level="user",
        )
        self.database.adjust_storage(
            self.guild_id,
            "oakclan",
            ItemCategory.PREY.value,
            "mouse",
            1,
            source="roll_forage",
            metadata={"territory": "ridge"},
            user_id=2,
            access_level="mod",
        )

        self.database.log_command_event(
            self.guild_id,
            "roll_forage",
            user_id=1,
            access_level="user",
            clan_name="birchclan",
            details={"category": ItemCategory.HERB.value, "territory": "ridge"},
        )
        self.database.log_command_event(
            self.guild_id,
            "roll_forage",
            user_id=2,
            access_level="mod",
            clan_name="oakclan",
            details={"category": ItemCategory.PREY.value, "territory": "ridge"},
        )

        cleared = self.database.clear_audit_entries(
            self.guild_id,
            clan_name="birchclan",
            category=ItemCategory.HERB.value,
            territory_name="ridge",
        )

        self.assertEqual(cleared["storage_deleted"], 1)
        self.assertEqual(cleared["command_deleted"], 1)
        self.assertEqual(cleared["total_deleted"], 2)

        exported = self.database.export_audit_entries(self.guild_id, since_days=31)
        self.assertEqual(len(exported["storage_entries"]), 2)
        self.assertEqual(len(exported["command_entries"]), 1)
        self.assertEqual(exported["command_entries"][0]["clan_name"], "oakclan")

    def test_roll_ranges_default_and_custom(self) -> None:
        defaults = self.database.list_roll_ranges(self.guild_id)
        self.assertEqual(defaults[0], (-9999, 5, 0))
        self.assertEqual(defaults[-1], (21, 9999, 4))
        self.assertEqual(self.database.get_find_count_for_total(self.guild_id, 17), 3)

        custom_ranges = [
            (-10, 0, 0),
            (1, 8, 1),
            (9, 14, 3),
            (15, 9999, 5),
        ]
        self.database.set_roll_ranges(self.guild_id, custom_ranges)

        self.assertEqual(self.database.list_roll_ranges(self.guild_id), custom_ranges)
        self.assertEqual(self.database.get_find_count_for_total(self.guild_id, 10), 3)
        self.assertEqual(self.database.get_find_count_for_total(self.guild_id, 20), 5)

        self.database.reset_roll_ranges(self.guild_id)
        self.assertEqual(self.database.get_find_count_for_total(self.guild_id, 20), 3)

    def test_roll_ranges_reject_overlap(self) -> None:
        with self.assertRaises(ValueError):
            self.database.set_roll_ranges(
                self.guild_id,
                [
                    (0, 10, 1),
                    (10, 20, 2),
                ],
            )


if __name__ == "__main__":
    unittest.main()
