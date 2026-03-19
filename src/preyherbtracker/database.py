from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import sqlite3
from typing import Iterator

from preyherbtracker.models import CategoryThreshold, Clan, Item, ItemCategory, TerritoryItem, TrackingMode


AUDIT_RETENTION_DAYS = 31


BASE_ACCESS_RANKS: dict[str, int] = {
    "user": 0,
    "mod": 200,
    "admin": 300,
}


class Database:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS clans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    tracking_mode TEXT NOT NULL DEFAULT 'off',
                    tracking_link TEXT,
                    cat_count INTEGER NOT NULL DEFAULT 0,
                    alert_channel_id INTEGER,
                    alerts_enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, name)
                );

                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    required_stat INTEGER,
                    require_stat_input_override INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, category, name)
                );

                CREATE TABLE IF NOT EXISTS clan_storage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(clan_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS storage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    delta INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS territories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    clan_id INTEGER REFERENCES clans(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, name)
                );

                CREATE TABLE IF NOT EXISTS territory_items (
                    territory_id INTEGER NOT NULL REFERENCES territories(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    weight REAL NOT NULL DEFAULT 1.0,
                    PRIMARY KEY (territory_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS role_permissions (
                    guild_id INTEGER NOT NULL,
                    access_level TEXT NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, access_level, role_id)
                );

                CREATE TABLE IF NOT EXISTS user_permissions (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    access_level TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS command_access_overrides (
                    guild_id INTEGER NOT NULL,
                    command_name TEXT NOT NULL,
                    access_level TEXT NOT NULL,
                    PRIMARY KEY (guild_id, command_name)
                );

                CREATE TABLE IF NOT EXISTS access_impersonation (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    access_level TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS access_levels (
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, name)
                );

                CREATE TABLE IF NOT EXISTS clan_members (
                    clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    PRIMARY KEY (clan_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS clan_item_links (
                    clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    weight REAL NOT NULL DEFAULT 1.0,
                    seasonal_modifier REAL NOT NULL DEFAULT 1.0,
                    PRIMARY KEY (clan_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS command_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER,
                    access_level TEXT,
                    command_name TEXT NOT NULL,
                    clan_name TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS roll_ranges (
                    guild_id INTEGER NOT NULL,
                    min_total INTEGER NOT NULL,
                    max_total INTEGER NOT NULL,
                    find_count INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, min_total, max_total)
                );

                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    global_seasonal_modifier REAL NOT NULL DEFAULT 1.0
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, name)
                );

                CREATE TABLE IF NOT EXISTS clan_category_alerts (
                    clan_id INTEGER NOT NULL REFERENCES clans(id) ON DELETE CASCADE,
                    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
                    threshold REAL NOT NULL,
                    threshold_mode TEXT NOT NULL DEFAULT 'dynamic',
                    PRIMARY KEY (clan_id, category_id)
                );

                CREATE TABLE IF NOT EXISTS character_stats (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    stat_name TEXT NOT NULL,
                    stat_value INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, stat_name)
                );

                CREATE TABLE IF NOT EXISTS pending_category_deletions (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    category_name TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id, category_name)
                );
                """
            )
            self._migrate_command_names(connection)
            self._ensure_storage_history_columns(connection)
            self._ensure_clan_columns(connection)
            self._ensure_territory_columns(connection)
            self._ensure_territory_item_columns(connection)
            self._ensure_item_columns(connection)
            self._ensure_category_rows(connection)
            self._ensure_category_alert_mode_column(connection)
            self._ensure_territory_requirement_columns(connection)
            self._purge_old_audit_rows(connection)

    @staticmethod
    def _migrate_command_names(connection: sqlite3.Connection) -> None:
        command_name_migrations = {
            "seed_defaults": "test_seed_defaults",
            "seed_demo": "test_seed_demo",
        }
        for old_name, new_name in command_name_migrations.items():
            connection.execute(
                """
                INSERT INTO command_access_overrides (guild_id, command_name, access_level)
                SELECT guild_id, ?, access_level
                FROM command_access_overrides
                WHERE command_name = ?
                ON CONFLICT(guild_id, command_name) DO UPDATE SET access_level = excluded.access_level
                """,
                (new_name, old_name),
            )
            connection.execute(
                "DELETE FROM command_access_overrides WHERE command_name = ?",
                (old_name,),
            )

    @staticmethod
    def _normalize_access_level_name(access_level: str) -> str:
        normalized = access_level.strip().lower()
        if not normalized:
            raise ValueError("Access level cannot be empty")
        if not re.match(r"^[a-z][a-z0-9_]{1,31}$", normalized):
            raise ValueError("Access level name must be 2-32 chars, lowercase letters/numbers/underscore, and start with a letter")
        return normalized

    def _validate_access_level(self, guild_id: int, access_level: str) -> str:
        normalized = self._normalize_access_level_name(access_level)
        if normalized not in self.get_access_level_ranks(guild_id):
            raise ValueError(f"Unknown access level: {normalized}")
        return normalized

    @staticmethod
    def _ensure_storage_history_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(storage_history)").fetchall()
        }
        if "user_id" not in columns:
            connection.execute("ALTER TABLE storage_history ADD COLUMN user_id INTEGER")
        if "access_level" not in columns:
            connection.execute("ALTER TABLE storage_history ADD COLUMN access_level TEXT")

    @staticmethod
    def _ensure_clan_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(clans)").fetchall()
        }
        if "seasonal_modifier" not in columns:
            connection.execute("ALTER TABLE clans ADD COLUMN seasonal_modifier REAL NOT NULL DEFAULT 1.0")

    @staticmethod
    def _ensure_territory_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(territories)").fetchall()
        }
        if "channel_id" not in columns:
            connection.execute("ALTER TABLE territories ADD COLUMN channel_id INTEGER")

    @staticmethod
    def _ensure_territory_item_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(territory_items)").fetchall()
        }
        if "seasonal_modifier" not in columns:
            connection.execute("ALTER TABLE territory_items ADD COLUMN seasonal_modifier REAL NOT NULL DEFAULT 1.0")

    @staticmethod
    def _ensure_item_columns(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(items)").fetchall()
        }
        if "required_stat" not in columns:
            connection.execute("ALTER TABLE items ADD COLUMN required_stat INTEGER")
        if "require_stat_input_override" not in columns:
            connection.execute("ALTER TABLE items ADD COLUMN require_stat_input_override INTEGER")
        if "required_stat_name" not in columns:
            connection.execute("ALTER TABLE items ADD COLUMN required_stat_name TEXT")

    @staticmethod
    def _ensure_territory_requirement_columns(connection: sqlite3.Connection) -> None:
        guild_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(guild_settings)").fetchall()
        }
        if "require_linked_territory_global" not in guild_columns:
            connection.execute(
                "ALTER TABLE guild_settings ADD COLUMN require_linked_territory_global INTEGER NOT NULL DEFAULT 0"
            )
        if "require_stat_input_global" not in guild_columns:
            connection.execute(
                "ALTER TABLE guild_settings ADD COLUMN require_stat_input_global INTEGER NOT NULL DEFAULT 0"
            )

        category_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(categories)").fetchall()
        }
        if "require_linked_territory_override" not in category_columns:
            connection.execute("ALTER TABLE categories ADD COLUMN require_linked_territory_override INTEGER")
        if "require_stat_input_override" not in category_columns:
            connection.execute("ALTER TABLE categories ADD COLUMN require_stat_input_override INTEGER")

        item_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(items)").fetchall()
        }
        if "require_linked_territory_override" not in item_columns:
            connection.execute("ALTER TABLE items ADD COLUMN require_linked_territory_override INTEGER")

    @staticmethod
    def _purge_old_audit_rows(connection: sqlite3.Connection, *, retention_days: int = AUDIT_RETENTION_DAYS) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        connection.execute("DELETE FROM storage_history WHERE created_at < ?", (cutoff,))
        connection.execute("DELETE FROM command_events WHERE created_at < ?", (cutoff,))

    @staticmethod
    def _default_threshold_for_category(category_name: str) -> float:
        if category_name == "prey":
            return 1.0
        if category_name == "herb":
            return 0.5
        return 0.0

    @staticmethod
    def _ensure_category_alert_mode_column(connection: sqlite3.Connection) -> None:
        cols = {row["name"] for row in connection.execute("PRAGMA table_info(clan_category_alerts)").fetchall()}
        if "threshold_mode" not in cols:
            connection.execute(
                "ALTER TABLE clan_category_alerts ADD COLUMN threshold_mode TEXT NOT NULL DEFAULT 'dynamic'"
            )

    @classmethod
    def _ensure_category_rows(cls, connection: sqlite3.Connection) -> None:
        guild_rows = connection.execute(
            """
            SELECT guild_id FROM clans
            UNION
            SELECT guild_id FROM items
            UNION
            SELECT guild_id FROM territories
            ORDER BY guild_id
            """
        ).fetchall()
        guild_ids = [int(row["guild_id"]) for row in guild_rows]

        for guild_id in guild_ids:
            connection.execute(
                "INSERT OR IGNORE INTO categories (guild_id, name) VALUES (?, ?)",
                (guild_id, "prey"),
            )
            connection.execute(
                "INSERT OR IGNORE INTO categories (guild_id, name) VALUES (?, ?)",
                (guild_id, "herb"),
            )

        item_category_rows = connection.execute(
            "SELECT DISTINCT guild_id, category FROM items WHERE category IS NOT NULL AND TRIM(category) != ''"
        ).fetchall()
        for row in item_category_rows:
            connection.execute(
                "INSERT OR IGNORE INTO categories (guild_id, name) VALUES (?, ?)",
                (int(row["guild_id"]), str(row["category"]).lower()),
            )

        clan_rows = connection.execute("SELECT id, guild_id FROM clans").fetchall()
        for clan_row in clan_rows:
            clan_id = int(clan_row["id"])
            guild_id = int(clan_row["guild_id"])
            category_rows = connection.execute(
                "SELECT id, name FROM categories WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            for category_row in category_rows:
                category_name = str(category_row["name"])
                threshold = cls._default_threshold_for_category(category_name)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO clan_category_alerts (clan_id, category_id, threshold)
                    VALUES (?, ?, ?)
                    """,
                    (clan_id, int(category_row["id"]), threshold),
                )

    @staticmethod
    def _ensure_baseline_categories_for_guild(connection: sqlite3.Connection, guild_id: int) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO categories (guild_id, name) VALUES (?, ?)",
            (guild_id, "prey"),
        )
        connection.execute(
            "INSERT OR IGNORE INTO categories (guild_id, name) VALUES (?, ?)",
            (guild_id, "herb"),
        )

    def list_categories(self, guild_id: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM categories WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def category_exists(self, guild_id: int, category_name: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM categories WHERE guild_id = ? AND name = ?",
                (guild_id, category_name.lower()),
            ).fetchone()
        return row is not None

    def require_category(self, guild_id: int, category_name: str) -> str:
        normalized = category_name.lower().strip()
        if not normalized:
            raise ValueError("Category cannot be empty")
        with self.connect() as connection:
            count_row = connection.execute(
                "SELECT COUNT(*) AS count FROM categories WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if int(count_row["count"]) == 0:
                self._ensure_baseline_categories_for_guild(connection, guild_id)
        if not self.category_exists(guild_id, normalized):
            raise ValueError(f"Unknown category: {normalized}")
        return normalized

    def create_category(self, guild_id: int, category_name: str) -> str:
        normalized = category_name.lower().strip()
        if not normalized:
            raise ValueError("Category cannot be empty")
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO categories (guild_id, name) VALUES (?, ?)",
                (guild_id, normalized),
            )
            category_row = connection.execute(
                "SELECT id FROM categories WHERE guild_id = ? AND name = ?",
                (guild_id, normalized),
            ).fetchone()
            if category_row is None:
                raise RuntimeError("Failed to create category")
            category_id = int(category_row["id"])
            clan_rows = connection.execute(
                "SELECT id FROM clans WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            threshold = self._default_threshold_for_category(normalized)
            for clan_row in clan_rows:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO clan_category_alerts (clan_id, category_id, threshold)
                    VALUES (?, ?, ?)
                    """,
                    (int(clan_row["id"]), category_id, threshold),
                )
        return normalized

    def get_clan_category_alert_thresholds(self, guild_id: int, clan_name: str) -> dict[str, CategoryThreshold]:
        clan = self.require_clan(guild_id, clan_name)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT categories.name, clan_category_alerts.threshold, clan_category_alerts.threshold_mode
                FROM clan_category_alerts
                JOIN categories ON categories.id = clan_category_alerts.category_id
                WHERE clan_category_alerts.clan_id = ?
                ORDER BY categories.name
                """,
                (clan.id,),
            ).fetchall()
        return {
            str(row["name"]): CategoryThreshold(mode=str(row["threshold_mode"]), value=float(row["threshold"]))
            for row in rows
        }

    def set_clan_category_alert_threshold(self, guild_id: int, clan_name: str, category_name: str, threshold: float, *, mode: str = "dynamic") -> None:
        if mode not in ("static", "dynamic"):
            raise ValueError(f"Invalid threshold mode '{mode}': must be 'static' or 'dynamic'")
        clan = self.require_clan(guild_id, clan_name)
        normalized_category = self.require_category(guild_id, category_name)
        with self.connect() as connection:
            category_row = connection.execute(
                "SELECT id FROM categories WHERE guild_id = ? AND name = ?",
                (guild_id, normalized_category),
            ).fetchone()
            if category_row is None:
                raise ValueError(f"Unknown category: {normalized_category}")
            connection.execute(
                """
                INSERT INTO clan_category_alerts (clan_id, category_id, threshold, threshold_mode)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(clan_id, category_id) DO UPDATE SET threshold = excluded.threshold, threshold_mode = excluded.threshold_mode
                """,
                (clan.id, int(category_row["id"]), threshold, mode),
            )

    def request_category_removal(self, guild_id: int, user_id: int, category_name: str, *, ttl_minutes: int = 15) -> str:
        normalized = self.require_category(guild_id, category_name)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        expires_str = expires_at.strftime("%Y-%m-%d %H:%M:%S")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_category_deletions (guild_id, user_id, category_name, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, category_name) DO UPDATE SET expires_at = excluded.expires_at
                """,
                (guild_id, user_id, normalized, expires_str),
            )
        return expires_str

    def confirm_category_removal(self, guild_id: int, user_id: int, category_name: str) -> dict[str, int]:
        normalized = category_name.lower().strip()
        with self.connect() as connection:
            pending = connection.execute(
                """
                SELECT expires_at
                FROM pending_category_deletions
                WHERE guild_id = ? AND user_id = ? AND category_name = ?
                """,
                (guild_id, user_id, normalized),
            ).fetchone()
            if pending is None:
                raise ValueError("No pending removal request for that category")

            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            if str(pending["expires_at"]) < now_str:
                connection.execute(
                    "DELETE FROM pending_category_deletions WHERE guild_id = ? AND user_id = ? AND category_name = ?",
                    (guild_id, user_id, normalized),
                )
                raise ValueError("Removal confirmation expired; run category_remove_request again")

            category_count_row = connection.execute(
                "SELECT COUNT(*) AS count FROM categories WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if int(category_count_row["count"]) <= 1:
                raise ValueError("Cannot remove the last remaining category")

            item_ids = connection.execute(
                "SELECT id FROM items WHERE guild_id = ? AND category = ?",
                (guild_id, normalized),
            ).fetchall()
            item_id_values = [int(row["id"]) for row in item_ids]

            links_deleted = 0
            storage_rows_deleted = 0
            if item_id_values:
                placeholders = ", ".join("?" for _ in item_id_values)
                link_row = connection.execute(
                    f"SELECT COUNT(*) AS count FROM territory_items WHERE item_id IN ({placeholders})",
                    tuple(item_id_values),
                ).fetchone()
                storage_row = connection.execute(
                    f"SELECT COUNT(*) AS count FROM clan_storage WHERE item_id IN ({placeholders})",
                    tuple(item_id_values),
                ).fetchone()
                links_deleted = int(link_row["count"])
                storage_rows_deleted = int(storage_row["count"])

            item_count_row = connection.execute(
                "SELECT COUNT(*) AS count FROM items WHERE guild_id = ? AND category = ?",
                (guild_id, normalized),
            ).fetchone()
            items_deleted = int(item_count_row["count"])

            connection.execute(
                "DELETE FROM items WHERE guild_id = ? AND category = ?",
                (guild_id, normalized),
            )

            category_row = connection.execute(
                "SELECT id FROM categories WHERE guild_id = ? AND name = ?",
                (guild_id, normalized),
            ).fetchone()
            if category_row is not None:
                connection.execute(
                    "DELETE FROM clan_category_alerts WHERE category_id = ?",
                    (int(category_row["id"]),),
                )
            connection.execute(
                "DELETE FROM categories WHERE guild_id = ? AND name = ?",
                (guild_id, normalized),
            )
            connection.execute(
                "DELETE FROM pending_category_deletions WHERE guild_id = ? AND user_id = ? AND category_name = ?",
                (guild_id, user_id, normalized),
            )

        return {
            "items_deleted": items_deleted,
            "links_deleted": links_deleted,
            "storage_rows_deleted": storage_rows_deleted,
        }

    def create_clan(
        self,
        guild_id: int,
        name: str,
        tracking_mode: str = TrackingMode.OFF,
        tracking_link: str | None = None,
        cat_count: int = 0,
    ) -> Clan:
        with self.connect() as connection:
            self._ensure_baseline_categories_for_guild(connection, guild_id)
            cursor = connection.execute(
                """
                INSERT INTO clans (guild_id, name, tracking_mode, tracking_link, cat_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, name.lower(), tracking_mode, tracking_link, cat_count),
            )
            clan_id = cursor.lastrowid
            category_rows = connection.execute(
                "SELECT id, name FROM categories WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            for row in category_rows:
                category_name = str(row["name"])
                if category_name == "prey":
                    threshold = 1.0
                elif category_name == "herb":
                    threshold = 0.5
                else:
                    threshold = self._default_threshold_for_category(category_name)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO clan_category_alerts (clan_id, category_id, threshold)
                    VALUES (?, ?, ?)
                    """,
                    (int(clan_id), int(row["id"]), threshold),
                )
        clan = self.get_clan(guild_id, name)
        if clan is None or clan.id != clan_id:
            raise RuntimeError("Failed to create clan")
        return clan

    def get_clan(self, guild_id: int, name: str) -> Clan | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM clans WHERE guild_id = ? AND name = ?",
                (guild_id, name.lower()),
            ).fetchone()
        return self._row_to_clan(row) if row else None

    def list_clans(self, guild_id: int) -> list[Clan]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM clans WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            ).fetchall()
        return [self._row_to_clan(row) for row in rows]

    def delete_clan(self, guild_id: int, name: str) -> None:
        clan = self.require_clan(guild_id, name)
        with self.connect() as connection:
            connection.execute("DELETE FROM clans WHERE id = ?", (clan.id,))

    def rename_clan(self, guild_id: int, old_name: str, new_name: str) -> Clan:
        clan = self.require_clan(guild_id, old_name)
        normalized_new = new_name.lower().strip()
        if not normalized_new:
            raise ValueError("New clan name cannot be empty.")
        if self.get_clan(guild_id, normalized_new) is not None:
            raise ValueError(f"A clan named '{normalized_new}' already exists.")
        with self.connect() as connection:
            connection.execute("UPDATE clans SET name = ? WHERE id = ?", (normalized_new, clan.id))
        return self.require_clan(guild_id, normalized_new)

    def add_clan_member(self, guild_id: int, clan_name: str, user_id: int) -> None:
        clan = self.require_clan(guild_id, clan_name)
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO clan_members (clan_id, user_id) VALUES (?, ?)",
                (clan.id, user_id),
            )

    def remove_clan_member(self, guild_id: int, clan_name: str, user_id: int) -> bool:
        clan = self.require_clan(guild_id, clan_name)
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM clan_members WHERE clan_id = ? AND user_id = ?",
                (clan.id, user_id),
            )
            return cursor.rowcount > 0

    def list_clan_members(self, guild_id: int, clan_name: str) -> list[int]:
        clan = self.require_clan(guild_id, clan_name)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM clan_members WHERE clan_id = ? ORDER BY user_id",
                (clan.id,),
            ).fetchall()
        return [int(row["user_id"]) for row in rows]

    def list_member_clans(self, guild_id: int, user_id: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT clans.name
                FROM clan_members
                JOIN clans ON clans.id = clan_members.clan_id
                WHERE clans.guild_id = ? AND clan_members.user_id = ?
                ORDER BY clans.name
                """,
                (guild_id, user_id),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def user_has_clan_membership(self, guild_id: int, user_id: int, clan_name: str) -> bool:
        normalized_name = clan_name.lower()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM clan_members
                JOIN clans ON clans.id = clan_members.clan_id
                WHERE clans.guild_id = ? AND clans.name = ? AND clan_members.user_id = ?
                """,
                (guild_id, normalized_name, user_id),
            ).fetchone()
        return row is not None

    def update_clan_config(
        self,
        guild_id: int,
        clan_name: str,
        *,
        tracking_mode: str | None = None,
        tracking_link: str | None = None,
        cat_count: int | None = None,
        alert_channel_id: int | None = None,
        alerts_enabled: bool | None = None,
        seasonal_modifier: float | None = None,
        roll_log_channel_id: int | None = None,
    ) -> Clan:
        clan = self.require_clan(guild_id, clan_name)
        fields: list[str] = []
        values: list[object] = []
        if tracking_mode is not None:
            fields.append("tracking_mode = ?")
            values.append(tracking_mode)
        if tracking_link is not None:
            fields.append("tracking_link = ?")
            values.append(tracking_link)
        if cat_count is not None:
            fields.append("cat_count = ?")
            values.append(cat_count)
        if alert_channel_id is not None:
            fields.append("alert_channel_id = ?")
            values.append(alert_channel_id)
        if alerts_enabled is not None:
            fields.append("alerts_enabled = ?")
            values.append(int(alerts_enabled))
        if seasonal_modifier is not None:
            fields.append("seasonal_modifier = ?")
            values.append(seasonal_modifier)
        if roll_log_channel_id is not None:
            fields.append("roll_log_channel_id = ?")
            values.append(roll_log_channel_id)
        if not fields:
            return clan
        values.extend([guild_id, clan.name])
        with self.connect() as connection:
            connection.execute(
                f"UPDATE clans SET {', '.join(fields)} WHERE guild_id = ? AND name = ?",
                tuple(values),
            )
        return self.require_clan(guild_id, clan.name)

    def set_character_stat(self, guild_id: int, user_id: int, stat_name: str, stat_value: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO character_stats (guild_id, user_id, stat_name, stat_value) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, stat_name.lower().strip(), stat_value),
            )

    def get_character_stats(self, guild_id: int, user_id: int) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT stat_name, stat_value FROM character_stats WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchall()
        return {str(row["stat_name"]): int(row["stat_value"]) for row in rows}

    def get_character_stat(self, guild_id: int, user_id: int, stat_name: str) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT stat_value FROM character_stats WHERE guild_id = ? AND user_id = ? AND stat_name = ?",
                (guild_id, user_id, stat_name.lower().strip()),
            ).fetchone()
        return int(row["stat_value"]) if row else None

    def set_global_territory_requirement(self, guild_id: int, required: bool, *, force: bool = False) -> None:
        required_int = int(required)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO guild_settings (guild_id, global_seasonal_modifier, require_linked_territory_global)
                VALUES (?, 1.0, ?)
                ON CONFLICT(guild_id) DO UPDATE SET require_linked_territory_global = excluded.require_linked_territory_global
                """,
                (guild_id, required_int),
            )
            if force:
                connection.execute(
                    "UPDATE categories SET require_linked_territory_override = ? WHERE guild_id = ?",
                    (required_int, guild_id),
                )
                connection.execute(
                    "UPDATE items SET require_linked_territory_override = ? WHERE guild_id = ?",
                    (required_int, guild_id),
                )

    def set_category_territory_requirement(
        self,
        guild_id: int,
        category: str,
        required: bool,
        *,
        force: bool = False,
    ) -> str:
        normalized_category = self.require_category(guild_id, category)
        required_int = int(required)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE categories
                SET require_linked_territory_override = ?
                WHERE guild_id = ? AND name = ?
                """,
                (required_int, guild_id, normalized_category),
            )
            if force:
                connection.execute(
                    """
                    UPDATE items
                    SET require_linked_territory_override = ?
                    WHERE guild_id = ? AND category = ?
                    """,
                    (required_int, guild_id, normalized_category),
                )
        return normalized_category

    def set_item_territory_requirement(
        self,
        guild_id: int,
        category: str,
        item_name: str,
        required: bool,
    ) -> Item:
        item = self.require_item(guild_id, category, item_name)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE items
                SET require_linked_territory_override = ?
                WHERE guild_id = ? AND category = ? AND name = ?
                """,
                (int(required), guild_id, item.category, item.name),
            )
        return self.require_item(guild_id, item.category, item.name)

    def get_global_territory_requirement(self, guild_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT require_linked_territory_global FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            return False
        return bool(row["require_linked_territory_global"])

    def get_effective_item_territory_requirements(self, guild_id: int, category: str) -> dict[str, bool]:
        normalized_category = self.require_category(guild_id, category)
        with self.connect() as connection:
            global_row = connection.execute(
                "SELECT require_linked_territory_global FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            global_required = bool(global_row["require_linked_territory_global"]) if global_row is not None else False
            category_row = connection.execute(
                "SELECT require_linked_territory_override FROM categories WHERE guild_id = ? AND name = ?",
                (guild_id, normalized_category),
            ).fetchone()
            category_override = (
                None
                if category_row is None or category_row["require_linked_territory_override"] is None
                else bool(category_row["require_linked_territory_override"])
            )
            rows = connection.execute(
                """
                SELECT name, require_linked_territory_override
                FROM items
                WHERE guild_id = ? AND category = ? AND enabled = 1
                ORDER BY name
                """,
                (guild_id, normalized_category),
            ).fetchall()
        default_required = category_override if category_override is not None else global_required
        result: dict[str, bool] = {}
        for row in rows:
            item_override = row["require_linked_territory_override"]
            if item_override is None:
                result[str(row["name"])] = bool(default_required)
            else:
                result[str(row["name"])] = bool(item_override)
        return result

    def set_global_stat_requirement(self, guild_id: int, required: bool, *, force: bool = False) -> None:
        required_int = int(required)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, global_seasonal_modifier, require_linked_territory_global, require_stat_input_global
                )
                VALUES (?, 1.0, 0, ?)
                ON CONFLICT(guild_id) DO UPDATE SET require_stat_input_global = excluded.require_stat_input_global
                """,
                (guild_id, required_int),
            )
            if force:
                connection.execute(
                    "UPDATE categories SET require_stat_input_override = ? WHERE guild_id = ?",
                    (required_int, guild_id),
                )
                connection.execute(
                    "UPDATE items SET require_stat_input_override = ? WHERE guild_id = ?",
                    (required_int, guild_id),
                )

    def set_category_stat_requirement(
        self,
        guild_id: int,
        category: str,
        required: bool,
        *,
        force: bool = False,
    ) -> str:
        normalized_category = self.require_category(guild_id, category)
        required_int = int(required)
        with self.connect() as connection:
            connection.execute(
                "UPDATE categories SET require_stat_input_override = ? WHERE guild_id = ? AND name = ?",
                (required_int, guild_id, normalized_category),
            )
            if force:
                connection.execute(
                    "UPDATE items SET require_stat_input_override = ? WHERE guild_id = ? AND category = ?",
                    (required_int, guild_id, normalized_category),
                )
        return normalized_category

    def set_item_stat_requirement(
        self,
        guild_id: int,
        category: str,
        item_name: str,
        required: bool,
    ) -> Item:
        item = self.require_item(guild_id, category, item_name)
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE items
                SET require_stat_input_override = ?
                WHERE guild_id = ? AND category = ? AND name = ?
                """,
                (int(required), guild_id, item.category, item.name),
            )
        return self.require_item(guild_id, item.category, item.name)

    def get_global_stat_requirement(self, guild_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT require_stat_input_global FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            return False
        return bool(row["require_stat_input_global"])

    def get_effective_item_stat_input_requirements(self, guild_id: int, category: str) -> dict[str, bool]:
        normalized_category = self.require_category(guild_id, category)
        with self.connect() as connection:
            global_row = connection.execute(
                "SELECT require_stat_input_global FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            global_required = bool(global_row["require_stat_input_global"]) if global_row is not None else False
            category_row = connection.execute(
                "SELECT require_stat_input_override FROM categories WHERE guild_id = ? AND name = ?",
                (guild_id, normalized_category),
            ).fetchone()
            category_override = (
                None
                if category_row is None or category_row["require_stat_input_override"] is None
                else bool(category_row["require_stat_input_override"])
            )
            rows = connection.execute(
                """
                SELECT name, require_stat_input_override
                FROM items
                WHERE guild_id = ? AND category = ? AND enabled = 1
                ORDER BY name
                """,
                (guild_id, normalized_category),
            ).fetchall()
        default_required = category_override if category_override is not None else global_required
        result: dict[str, bool] = {}
        for row in rows:
            item_override = row["require_stat_input_override"]
            if item_override is None:
                result[str(row["name"])] = bool(default_required)
            else:
                result[str(row["name"])] = bool(item_override)
        return result

    def add_item(
        self,
        guild_id: int,
        name: str,
        category: str,
        *,
        enabled: bool = True,
        is_default: bool = False,
        required_stat: int | None = None,
        required_stat_name: str | None = None,
    ) -> Item:
        normalized_name = name.lower()
        normalized_category = self.require_category(guild_id, category)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO items (
                    id, guild_id, name, category, enabled, is_default,
                    required_stat, required_stat_name
                )
                VALUES (
                    COALESCE((SELECT id FROM items WHERE guild_id = ? AND category = ? AND name = ?), NULL),
                    ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    guild_id,
                    normalized_category,
                    normalized_name,
                    guild_id,
                    normalized_name,
                    normalized_category,
                    int(enabled),
                    int(is_default),
                    required_stat,
                    required_stat_name.lower().strip() if required_stat_name else None,
                ),
            )
        item = self.get_item(guild_id, normalized_category, normalized_name)
        if item is None:
            raise RuntimeError("Failed to upsert item")
        return item

    def get_item(self, guild_id: int, category: str, name: str) -> Item | None:
        normalized_category = category.lower().strip()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE guild_id = ? AND category = ? AND name = ?",
                (guild_id, normalized_category, name.lower()),
            ).fetchone()
        return self._row_to_item(row) if row else None

    def remove_item(self, guild_id: int, category: str, name: str) -> bool:
        normalized_category = self.require_category(guild_id, category)
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM items WHERE guild_id = ? AND category = ? AND name = ?",
                (guild_id, normalized_category, name.lower()),
            )
            return cursor.rowcount > 0

    def list_items(
        self,
        guild_id: int,
        category: str | None = None,
        *,
        enabled_only: bool = True,
    ) -> list[Item]:
        query = "SELECT * FROM items WHERE guild_id = ?"
        values: list[object] = [guild_id]
        if category is not None:
            normalized_category = category.lower().strip()
            query += " AND category = ?"
            values.append(normalized_category)
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY category, name"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return [self._row_to_item(row) for row in rows]

    def update_item(
        self,
        guild_id: int,
        category: str,
        name: str,
        *,
        new_name: str | None = None,
        enabled: bool | None = None,
        required_stat: int | None = None,
        required_stat_name: str | None = None,
    ) -> Item:
        normalized_category = self.require_category(guild_id, category)
        item = self.require_item(guild_id, normalized_category, name)
        fields: list[str] = []
        values: list[object] = []
        if new_name is not None:
            fields.append("name = ?")
            values.append(new_name.lower())
        if enabled is not None:
            fields.append("enabled = ?")
            values.append(int(enabled))
        if required_stat is not None:
            fields.append("required_stat = ?")
            values.append(required_stat)
        if required_stat_name is not None:
            fields.append("required_stat_name = ?")
            values.append(required_stat_name.lower().strip() if required_stat_name else None)
        if not fields:
            return item
        values.extend([guild_id, normalized_category, item.name])
        with self.connect() as connection:
            connection.execute(
                f"UPDATE items SET {', '.join(fields)} WHERE guild_id = ? AND category = ? AND name = ?",
                tuple(values),
            )
        final_name = new_name.lower() if new_name else item.name
        result = self.get_item(guild_id, normalized_category, final_name)
        if result is None:
            raise RuntimeError("Failed to update item")
        return result

    def create_territory(
        self,
        guild_id: int,
        name: str,
        clan_name: str | None = None,
        *,
        channel_id: int | None = None,
    ) -> int:
        clan_id = None
        if clan_name:
            clan_id = self.require_clan(guild_id, clan_name).id
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO territories (guild_id, clan_id, name, channel_id) VALUES (?, ?, ?, ?)",
                (guild_id, clan_id, name.lower(), channel_id),
            )
            territory_id = int(cursor.lastrowid)
            if clan_id is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO territory_items (territory_id, item_id, weight, seasonal_modifier)
                    SELECT ?, item_id, weight, seasonal_modifier
                    FROM clan_item_links
                    WHERE clan_id = ?
                    """,
                    (territory_id, clan_id),
                )
            return territory_id

    def get_territory(self, guild_id: int, name: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM territories WHERE guild_id = ? AND name = ?",
                (guild_id, name.lower()),
            ).fetchone()

    def list_territory_names(self, guild_id: int) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM territories WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def list_territory_channel_links(self, guild_id: int) -> list[tuple[str, int | None]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name, channel_id FROM territories WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            ).fetchall()
        return [
            (str(row["name"]), int(row["channel_id"]) if row["channel_id"] is not None else None)
            for row in rows
        ]

    @staticmethod
    def default_roll_ranges() -> list[tuple[int, int, int]]:
        return [
            (-9999, 5, 0),
            (6, 10, 1),
            (11, 15, 2),
            (16, 20, 3),
            (21, 9999, 4),
        ]

    def list_roll_ranges(self, guild_id: int) -> list[tuple[int, int, int]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT min_total, max_total, find_count
                FROM roll_ranges
                WHERE guild_id = ?
                ORDER BY min_total, max_total
                """,
                (guild_id,),
            ).fetchall()
        if not rows:
            return self.default_roll_ranges()
        return [(int(row["min_total"]), int(row["max_total"]), int(row["find_count"])) for row in rows]

    def set_roll_ranges(self, guild_id: int, ranges: list[tuple[int, int, int]]) -> None:
        if not ranges:
            raise ValueError("At least one roll range is required")
        normalized = sorted(ranges, key=lambda entry: (entry[0], entry[1]))
        for min_total, max_total, find_count in normalized:
            if min_total > max_total:
                raise ValueError("Range minimum cannot exceed maximum")
            if find_count < 0:
                raise ValueError("Find count cannot be negative")
        for index in range(1, len(normalized)):
            prev_max = normalized[index - 1][1]
            current_min = normalized[index][0]
            if current_min <= prev_max:
                raise ValueError("Roll ranges cannot overlap")

        with self.connect() as connection:
            connection.execute("DELETE FROM roll_ranges WHERE guild_id = ?", (guild_id,))
            connection.executemany(
                "INSERT INTO roll_ranges (guild_id, min_total, max_total, find_count) VALUES (?, ?, ?, ?)",
                [(guild_id, min_total, max_total, find_count) for min_total, max_total, find_count in normalized],
            )

    def add_roll_range(self, guild_id: int, min_total: int, max_total: int, find_count: int) -> None:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT min_total, max_total, find_count
                FROM roll_ranges
                WHERE guild_id = ?
                ORDER BY min_total, max_total
                """,
                (guild_id,),
            ).fetchall()
        ranges = [(int(row["min_total"]), int(row["max_total"]), int(row["find_count"])) for row in rows]
        if not ranges:
            ranges = self.default_roll_ranges()
        ranges.append((min_total, max_total, find_count))
        self.set_roll_ranges(guild_id, ranges)

    def reset_roll_ranges(self, guild_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM roll_ranges WHERE guild_id = ?", (guild_id,))

    def get_find_count_for_total(self, guild_id: int, total: int) -> int:
        ranges = self.list_roll_ranges(guild_id)
        for min_total, max_total, find_count in ranges:
            if min_total <= total <= max_total:
                return find_count
        # If staff set non-covering ranges, fall back to default behavior.
        for min_total, max_total, find_count in self.default_roll_ranges():
            if min_total <= total <= max_total:
                return find_count
        return 0

    def set_territory_channel_link(self, guild_id: int, territory_name: str, channel_id: int | None) -> None:
        territory = self.get_territory(guild_id, territory_name)
        if territory is None:
            raise ValueError(f"Unknown territory: {territory_name}")
        with self.connect() as connection:
            connection.execute(
                "UPDATE territories SET channel_id = ? WHERE guild_id = ? AND name = ?",
                (channel_id, guild_id, territory_name.lower()),
            )

    def get_territory_channel_link(self, guild_id: int, territory_name: str) -> int | None:
        territory = self.get_territory(guild_id, territory_name)
        if territory is None:
            raise ValueError(f"Unknown territory: {territory_name}")
        value = territory["channel_id"]
        return int(value) if value is not None else None

    def detect_territory_from_channels(
        self,
        guild_id: int,
        channel_ids: list[int],
        channel_names: list[str],
        *,
        allow_name_fallback: bool = True,
    ) -> str | None:
        if channel_ids:
            placeholders = ", ".join("?" for _ in channel_ids)
            with self.connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT name
                    FROM territories
                    WHERE guild_id = ? AND channel_id IN ({placeholders})
                    ORDER BY CASE WHEN channel_id = ? THEN 0 ELSE 1 END, name
                    LIMIT 1
                    """,
                    (guild_id, *channel_ids, channel_ids[0]),
                ).fetchone()
            if row is not None:
                return str(row["name"])

        if not allow_name_fallback:
            return None

        # Fallback: exact name match from current channel/thread or its parent names.
        normalized_names = [name.lower() for name in channel_names if name]
        if not normalized_names:
            return None
        placeholders = ", ".join("?" for _ in normalized_names)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT name
                FROM territories
                WHERE guild_id = ? AND name IN ({placeholders})
                ORDER BY name
                LIMIT 1
                """,
                (guild_id, *normalized_names),
            ).fetchone()
        return str(row["name"]) if row is not None else None

    def set_territory_item_weight(
        self,
        guild_id: int,
        territory_name: str,
        category: str,
        item_name: str,
        weight: float,
        *,
        seasonal_modifier: float = 1.0,
    ) -> None:
        territory = self.get_territory(guild_id, territory_name)
        if territory is None:
            raise ValueError(f"Unknown territory: {territory_name}")
        item = self.require_item(guild_id, category, item_name)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO territory_items (territory_id, item_id, weight, seasonal_modifier)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(territory_id, item_id) DO UPDATE SET
                    weight = excluded.weight,
                    seasonal_modifier = excluded.seasonal_modifier
                """,
                (territory["id"], item.id, weight, seasonal_modifier),
            )

    def list_territory_names_by_clan(self, guild_id: int, clan_name: str) -> list[str]:
        clan = self.require_clan(guild_id, clan_name)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM territories WHERE clan_id = ? ORDER BY name",
                (clan.id,),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def get_global_seasonal_modifier(self, guild_id: int) -> float:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT global_seasonal_modifier FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return float(row["global_seasonal_modifier"]) if row else 1.0

    def set_global_seasonal_modifier(self, guild_id: int, modifier: float) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO guild_settings (guild_id, global_seasonal_modifier)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET global_seasonal_modifier = excluded.global_seasonal_modifier
                """,
                (guild_id, modifier),
            )

    def get_seasonal_modifiers_for_territory(self, guild_id: int, territory_name: str) -> tuple[float, float]:
        """Returns (clan_seasonal_modifier, global_seasonal_modifier) for a territory."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(clans.seasonal_modifier, 1.0) AS clan_seasonal_modifier
                FROM territories
                LEFT JOIN clans ON clans.id = territories.clan_id
                WHERE territories.guild_id = ? AND territories.name = ?
                """,
                (guild_id, territory_name.lower()),
            ).fetchone()
        clan_seasonal = float(row["clan_seasonal_modifier"]) if row else 1.0
        global_seasonal = self.get_global_seasonal_modifier(guild_id)
        return clan_seasonal, global_seasonal

    def get_weight_breakdown(
        self,
        guild_id: int,
        clan_name: str,
        *,
        category: str | None = None,
        territory_name: str | None = None,
        item_name: str | None = None,
    ) -> list[dict[str, object]]:
        clan = self.require_clan(guild_id, clan_name)
        global_seasonal = self.get_global_seasonal_modifier(guild_id)
        query = """
            SELECT
                territories.name AS territory_name,
                items.name AS item_name,
                items.category,
                territory_items.weight,
                territory_items.seasonal_modifier AS territory_seasonal_modifier
            FROM territory_items
            JOIN territories ON territories.id = territory_items.territory_id
            JOIN items ON items.id = territory_items.item_id
            WHERE territories.clan_id = ?
              AND items.enabled = 1
        """
        values: list[object] = [clan.id]
        if territory_name:
            query += " AND territories.name = ?"
            values.append(territory_name.lower())
        if category:
            query += " AND items.category = ?"
            values.append(category)
        if item_name:
            query += " AND items.name = ?"
            values.append(item_name.lower())
        query += " ORDER BY territories.name, items.category, items.name"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        result: list[dict[str, object]] = []
        cs = clan.seasonal_modifier
        gs = global_seasonal
        for row in rows:
            w = float(row["weight"])
            ts = float(row["territory_seasonal_modifier"])
            eff = w * ts * cs * gs
            result.append({
                "territory_name": str(row["territory_name"]),
                "item_name": str(row["item_name"]),
                "category": str(row["category"]),
                "weight": w,
                "territory_seasonal_modifier": ts,
                "clan_seasonal_modifier": cs,
                "global_seasonal_modifier": gs,
                "effective_weight": eff,
            })
        return result

    def list_territory_items(self, guild_id: int, territory_name: str, category: str) -> list[TerritoryItem]:
        territory = self.get_territory(guild_id, territory_name)
        if territory is None:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT items.name, territory_items.weight, territory_items.seasonal_modifier
                FROM territory_items
                JOIN items ON items.id = territory_items.item_id
                WHERE territory_items.territory_id = ? AND items.category = ? AND items.enabled = 1
                ORDER BY items.name
                """,
                (territory["id"], category),
            ).fetchall()
        return [
            TerritoryItem(
                item_name=row["name"],
                weight=float(row["weight"]),
                seasonal_modifier=float(row["seasonal_modifier"]),
            )
            for row in rows
        ]

    def list_all_territory_item_links(self, guild_id: int) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    territories.name AS territory_name,
                    clans.name AS clan_name,
                    items.category AS category,
                    items.name AS item_name,
                    territory_items.weight AS weight,
                    territory_items.seasonal_modifier AS seasonal_modifier
                FROM territory_items
                JOIN territories ON territories.id = territory_items.territory_id
                LEFT JOIN clans ON clans.id = territories.clan_id
                JOIN items ON items.id = territory_items.item_id
                WHERE territories.guild_id = ?
                ORDER BY territories.name, items.category, items.name
                """,
                (guild_id,),
            ).fetchall()
        return [
            {
                "territory_name": str(row["territory_name"]),
                "clan_name": str(row["clan_name"]) if row["clan_name"] is not None else None,
                "category": str(row["category"]),
                "item_name": str(row["item_name"]),
                "weight": float(row["weight"]),
                "seasonal_modifier": float(row["seasonal_modifier"]),
                "effective_weight": float(row["weight"]) * float(row["seasonal_modifier"]),
            }
            for row in rows
        ]

    def remove_territory_item(self, guild_id: int, territory_name: str, category: str, item_name: str) -> bool:
        territory = self.get_territory(guild_id, territory_name)
        if territory is None:
            raise ValueError(f"Unknown territory: {territory_name}")
        item = self.get_item(guild_id, category, item_name)
        if item is None:
            return False
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM territory_items WHERE territory_id = ? AND item_id = ?",
                (territory["id"], item.id),
            )
            return cursor.rowcount > 0

    def list_item_links(self, guild_id: int, category: str, item_name: str | None = None) -> list[dict[str, object]]:
        query = """
            SELECT
                territories.name AS territory_name,
                items.name AS item_name,
                territory_items.weight AS weight,
                territory_items.seasonal_modifier AS seasonal_modifier
            FROM territory_items
            JOIN territories ON territories.id = territory_items.territory_id
            JOIN items ON items.id = territory_items.item_id
            WHERE territories.guild_id = ? AND items.category = ?
        """
        values: list[object] = [guild_id, category]
        if item_name:
            query += " AND items.name = ?"
            values.append(item_name.lower())
        query += " ORDER BY items.name, territories.name"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return [
            {
                "territory_name": str(row["territory_name"]),
                "item_name": str(row["item_name"]),
                "weight": float(row["weight"]),
                "seasonal_modifier": float(row["seasonal_modifier"]),
                "effective_weight": float(row["weight"]) * float(row["seasonal_modifier"]),
            }
            for row in rows
        ]

    def set_clan_item_link(
        self,
        guild_id: int,
        clan_name: str,
        category: str,
        item_name: str,
        weight: float,
        *,
        seasonal_modifier: float = 1.0,
    ) -> None:
        clan = self.require_clan(guild_id, clan_name)
        item = self.require_item(guild_id, category, item_name)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO clan_item_links (clan_id, item_id, weight, seasonal_modifier)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(clan_id, item_id) DO UPDATE SET
                    weight = excluded.weight,
                    seasonal_modifier = excluded.seasonal_modifier
                """,
                (clan.id, item.id, weight, seasonal_modifier),
            )
            # Propagate updated weight to all territories already in this clan.
            connection.execute(
                """
                INSERT INTO territory_items (territory_id, item_id, weight, seasonal_modifier)
                SELECT territories.id, ?, ?, ?
                FROM territories
                WHERE territories.clan_id = ?
                ON CONFLICT(territory_id, item_id) DO UPDATE SET
                    weight = excluded.weight,
                    seasonal_modifier = excluded.seasonal_modifier
                """,
                (item.id, weight, seasonal_modifier, clan.id),
            )

    def list_clan_item_links(self, guild_id: int, clan_name: str) -> list[dict[str, object]]:
        clan = self.require_clan(guild_id, clan_name)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT items.category, items.name AS item_name,
                       clan_item_links.weight, clan_item_links.seasonal_modifier
                FROM clan_item_links
                JOIN items ON items.id = clan_item_links.item_id
                WHERE clan_item_links.clan_id = ?
                ORDER BY items.category, items.name
                """,
                (clan.id,),
            ).fetchall()
        return [
            {
                "category": str(row["category"]),
                "item_name": str(row["item_name"]),
                "weight": float(row["weight"]),
                "seasonal_modifier": float(row["seasonal_modifier"]),
            }
            for row in rows
        ]

    def adjust_storage(
        self,
        guild_id: int,
        clan_name: str,
        category: str,
        item_name: str,
        delta: int,
        *,
        source: str,
        metadata: dict[str, object] | None = None,
        user_id: int | None = None,
        access_level: str | None = None,
    ) -> int:
        clan = self.require_clan(guild_id, clan_name)
        item = self.require_item(guild_id, category, item_name)
        with self.connect() as connection:
            self._purge_old_audit_rows(connection)
            current_row = connection.execute(
                "SELECT quantity FROM clan_storage WHERE clan_id = ? AND item_id = ?",
                (clan.id, item.id),
            ).fetchone()
            current_quantity = int(current_row["quantity"]) if current_row else 0
            next_quantity = current_quantity + delta
            if next_quantity < 0:
                raise ValueError(f"Cannot reduce {item_name} below 0")
            connection.execute(
                """
                INSERT INTO clan_storage (clan_id, item_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(clan_id, item_id)
                DO UPDATE SET quantity = excluded.quantity, updated_at = CURRENT_TIMESTAMP
                """,
                (clan.id, item.id, next_quantity),
            )
            connection.execute(
                "INSERT INTO storage_history (clan_id, item_id, delta, source, metadata, user_id, access_level) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (clan.id, item.id, delta, source, json.dumps(metadata or {}), user_id, access_level),
            )
        return next_quantity

    def set_storage(
        self,
        guild_id: int,
        clan_name: str,
        category: str,
        item_name: str,
        quantity: int,
        *,
        source: str,
        user_id: int | None = None,
        access_level: str | None = None,
    ) -> int:
        clan = self.require_clan(guild_id, clan_name)
        item = self.require_item(guild_id, category, item_name)
        with self.connect() as connection:
            self._purge_old_audit_rows(connection)
            current_row = connection.execute(
                "SELECT quantity FROM clan_storage WHERE clan_id = ? AND item_id = ?",
                (clan.id, item.id),
            ).fetchone()
            current_quantity = int(current_row["quantity"]) if current_row else 0
            delta = quantity - current_quantity
            connection.execute(
                """
                INSERT INTO clan_storage (clan_id, item_id, quantity)
                VALUES (?, ?, ?)
                ON CONFLICT(clan_id, item_id)
                DO UPDATE SET quantity = excluded.quantity, updated_at = CURRENT_TIMESTAMP
                """,
                (clan.id, item.id, quantity),
            )
            connection.execute(
                "INSERT INTO storage_history (clan_id, item_id, delta, source, metadata, user_id, access_level) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (clan.id, item.id, delta, source, json.dumps({"set_to": quantity}), user_id, access_level),
            )
        return quantity

    def log_command_event(
        self,
        guild_id: int,
        command_name: str,
        *,
        user_id: int | None = None,
        access_level: str | None = None,
        clan_name: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        normalized_clan_name = clan_name.lower() if clan_name else None
        with self.connect() as connection:
            self._purge_old_audit_rows(connection)
            connection.execute(
                """
                INSERT INTO command_events (guild_id, user_id, access_level, command_name, clan_name, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, access_level, command_name, normalized_clan_name, json.dumps(details or {})),
            )

    def get_audit_entries(
        self,
        guild_id: int,
        *,
        viewer_access_level: str,
        visible_clans: list[str] | None = None,
        clan_name: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        normalized_clan_name = clan_name.lower() if clan_name else None
        normalized_visible_clans = [name.lower() for name in visible_clans] if visible_clans else None
        access_ranks = self.get_access_level_ranks(guild_id)
        viewer_rank = access_ranks.get(viewer_access_level, BASE_ACCESS_RANKS["user"])
        allowed_access_levels = [
            access_level for access_level, rank in access_ranks.items() if rank <= viewer_rank
        ] + [None]

        storage_query = """
            SELECT
                storage_history.created_at,
                storage_history.user_id,
                storage_history.access_level,
                storage_history.source,
                storage_history.delta,
                storage_history.metadata,
                clans.name AS clan_name,
                items.name AS item_name,
                items.category AS category
            FROM storage_history
            JOIN clans ON clans.id = storage_history.clan_id
            JOIN items ON items.id = storage_history.item_id
            WHERE clans.guild_id = ?
        """
        storage_values: list[object] = [guild_id]
        if normalized_clan_name:
            storage_query += " AND clans.name = ?"
            storage_values.append(normalized_clan_name)
        if normalized_visible_clans is not None:
            placeholders = ", ".join("?" for _ in normalized_visible_clans)
            storage_query += f" AND clans.name IN ({placeholders})"
            storage_values.extend(normalized_visible_clans)
        non_null_levels = [level for level in allowed_access_levels if level is not None]
        placeholders = ", ".join("?" for _ in non_null_levels)
        storage_query += f" AND (storage_history.access_level IN ({placeholders}) OR storage_history.access_level IS NULL)"
        storage_values.extend(non_null_levels)
        storage_query += " ORDER BY storage_history.created_at DESC LIMIT ?"
        storage_values.append(limit)

        with self.connect() as connection:
            self._purge_old_audit_rows(connection)
            storage_rows = connection.execute(storage_query, tuple(storage_values)).fetchall()

            command_rows: list[sqlite3.Row] = []
            if viewer_rank >= access_ranks.get("mod", BASE_ACCESS_RANKS["mod"]):
                command_query = """
                    SELECT created_at, user_id, access_level, command_name, clan_name, details
                    FROM command_events
                    WHERE guild_id = ?
                """
                command_values: list[object] = [guild_id]
                if normalized_clan_name:
                    command_query += " AND clan_name = ?"
                    command_values.append(normalized_clan_name)
                non_null_levels = [level for level in allowed_access_levels if level is not None]
                placeholders = ", ".join("?" for _ in non_null_levels)
                command_query += f" AND (access_level IN ({placeholders}) OR access_level IS NULL)"
                command_values.extend(non_null_levels)
                command_query += " ORDER BY created_at DESC LIMIT ?"
                command_values.append(limit)
                command_rows = connection.execute(command_query, tuple(command_values)).fetchall()

        entries: list[dict[str, object]] = []
        for row in storage_rows:
            metadata = json.loads(str(row["metadata"] or "{}"))
            entries.append(
                {
                    "kind": "storage",
                    "created_at": str(row["created_at"]),
                    "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
                    "access_level": str(row["access_level"]) if row["access_level"] is not None else "unknown",
                    "clan_name": str(row["clan_name"]),
                    "item_name": str(row["item_name"]),
                    "category": str(row["category"]),
                    "delta": int(row["delta"]),
                    "source": str(row["source"]),
                    "details": metadata,
                }
            )
        for row in command_rows:
            details = json.loads(str(row["details"] or "{}"))
            entries.append(
                {
                    "kind": "command",
                    "created_at": str(row["created_at"]),
                    "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
                    "access_level": str(row["access_level"]) if row["access_level"] is not None else "unknown",
                    "command_name": str(row["command_name"]),
                    "clan_name": str(row["clan_name"]) if row["clan_name"] is not None else None,
                    "details": details,
                }
            )
        entries.sort(key=lambda entry: str(entry["created_at"]), reverse=True)
        return entries[:limit]

    def export_audit_entries(
        self,
        guild_id: int,
        *,
        since_days: int,
        clan_name: str | None = None,
    ) -> dict[str, object]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y-%m-%d %H:%M:%S")
        normalized_clan_name = clan_name.lower() if clan_name else None

        storage_query = """
            SELECT
                storage_history.id,
                storage_history.created_at,
                storage_history.user_id,
                storage_history.access_level,
                storage_history.source,
                storage_history.delta,
                storage_history.metadata,
                clans.name AS clan_name,
                items.name AS item_name,
                items.category AS category
            FROM storage_history
            JOIN clans ON clans.id = storage_history.clan_id
            JOIN items ON items.id = storage_history.item_id
            WHERE clans.guild_id = ? AND storage_history.created_at >= ?
        """
        storage_values: list[object] = [guild_id, cutoff]
        if normalized_clan_name:
            storage_query += " AND clans.name = ?"
            storage_values.append(normalized_clan_name)
        storage_query += " ORDER BY storage_history.created_at DESC"

        command_query = """
            SELECT id, created_at, user_id, access_level, command_name, clan_name, details
            FROM command_events
            WHERE guild_id = ? AND created_at >= ?
        """
        command_values: list[object] = [guild_id, cutoff]
        if normalized_clan_name:
            command_query += " AND clan_name = ?"
            command_values.append(normalized_clan_name)
        command_query += " ORDER BY created_at DESC"

        with self.connect() as connection:
            self._purge_old_audit_rows(connection)
            storage_rows = connection.execute(storage_query, tuple(storage_values)).fetchall()
            command_rows = connection.execute(command_query, tuple(command_values)).fetchall()

        storage_entries = [
            {
                "id": int(row["id"]),
                "created_at": str(row["created_at"]),
                "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
                "access_level": str(row["access_level"]) if row["access_level"] is not None else None,
                "source": str(row["source"]),
                "delta": int(row["delta"]),
                "metadata": json.loads(str(row["metadata"] or "{}")),
                "clan_name": str(row["clan_name"]),
                "item_name": str(row["item_name"]),
                "category": str(row["category"]),
            }
            for row in storage_rows
        ]
        command_entries = [
            {
                "id": int(row["id"]),
                "created_at": str(row["created_at"]),
                "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
                "access_level": str(row["access_level"]) if row["access_level"] is not None else None,
                "command_name": str(row["command_name"]),
                "clan_name": str(row["clan_name"]) if row["clan_name"] is not None else None,
                "details": json.loads(str(row["details"] or "{}")),
            }
            for row in command_rows
        ]

        return {
            "guild_id": guild_id,
            "since_days": since_days,
            "cutoff_utc": cutoff,
            "clan_name": normalized_clan_name,
            "storage_entries": storage_entries,
            "command_entries": command_entries,
        }

    def clear_audit_entries(
        self,
        guild_id: int,
        *,
        clan_name: str | None = None,
        category: str | None = None,
        territory_name: str | None = None,
    ) -> dict[str, object]:
        normalized_clan = clan_name.lower().strip() if clan_name else None
        normalized_category = category.lower().strip() if category else None
        normalized_territory = territory_name.lower().strip() if territory_name else None

        def payload_matches(value: object, expected: str) -> bool:
            if isinstance(value, str):
                return value.lower().strip() == expected
            if isinstance(value, list):
                return any(isinstance(entry, str) and entry.lower().strip() == expected for entry in value)
            return False

        with self.connect() as connection:
            self._purge_old_audit_rows(connection)

            storage_query = """
                SELECT storage_history.id, storage_history.metadata
                FROM storage_history
                JOIN clans ON clans.id = storage_history.clan_id
                JOIN items ON items.id = storage_history.item_id
                WHERE clans.guild_id = ?
            """
            storage_values: list[object] = [guild_id]
            if normalized_clan is not None:
                storage_query += " AND clans.name = ?"
                storage_values.append(normalized_clan)
            if normalized_category is not None:
                storage_query += " AND items.category = ?"
                storage_values.append(normalized_category)
            storage_rows = connection.execute(storage_query, tuple(storage_values)).fetchall()

            storage_ids: list[int] = []
            for row in storage_rows:
                if normalized_territory is not None:
                    metadata = json.loads(str(row["metadata"] or "{}"))
                    if not payload_matches(metadata.get("territory"), normalized_territory):
                        continue
                storage_ids.append(int(row["id"]))

            if storage_ids:
                placeholders = ", ".join("?" for _ in storage_ids)
                connection.execute(f"DELETE FROM storage_history WHERE id IN ({placeholders})", tuple(storage_ids))

            command_query = """
                SELECT id, details
                FROM command_events
                WHERE guild_id = ?
            """
            command_values: list[object] = [guild_id]
            if normalized_clan is not None:
                command_query += " AND clan_name = ?"
                command_values.append(normalized_clan)
            command_rows = connection.execute(command_query, tuple(command_values)).fetchall()

            command_ids: list[int] = []
            for row in command_rows:
                details = json.loads(str(row["details"] or "{}"))
                if normalized_category is not None and not payload_matches(details.get("category"), normalized_category):
                    continue
                if normalized_territory is not None and not (
                    payload_matches(details.get("territory"), normalized_territory)
                    or payload_matches(details.get("territory_name"), normalized_territory)
                    or payload_matches(details.get("territories"), normalized_territory)
                ):
                    continue
                command_ids.append(int(row["id"]))

            if command_ids:
                placeholders = ", ".join("?" for _ in command_ids)
                connection.execute(f"DELETE FROM command_events WHERE id IN ({placeholders})", tuple(command_ids))

        return {
            "clan_name": normalized_clan,
            "category": normalized_category,
            "territory_name": normalized_territory,
            "storage_deleted": len(storage_ids),
            "command_deleted": len(command_ids),
            "total_deleted": len(storage_ids) + len(command_ids),
        }

    def get_latest_undoable_storage_entry(
        self,
        guild_id: int,
        *,
        viewer_access_level: str,
        viewer_user_id: int,
        clan_name: str,
        visible_clans: list[str] | None = None,
    ) -> dict[str, object] | None:
        normalized_clan_name = clan_name.lower()
        query = """
            SELECT
                storage_history.id,
                storage_history.created_at,
                storage_history.user_id,
                storage_history.access_level,
                storage_history.source,
                storage_history.delta,
                storage_history.metadata,
                clans.name AS clan_name,
                items.name AS item_name,
                items.category AS category
            FROM storage_history
            JOIN clans ON clans.id = storage_history.clan_id
            JOIN items ON items.id = storage_history.item_id
            WHERE clans.guild_id = ?
              AND clans.name = ?
              AND storage_history.source != 'audit_undo_last'
        """
        values: list[object] = [guild_id, normalized_clan_name]

        if visible_clans is not None:
            normalized_visible_clans = [name.lower() for name in visible_clans]
            placeholders = ", ".join("?" for _ in normalized_visible_clans)
            query += f" AND clans.name IN ({placeholders})"
            values.extend(normalized_visible_clans)

        access_ranks = self.get_access_level_ranks(guild_id)
        viewer_rank = access_ranks.get(viewer_access_level, BASE_ACCESS_RANKS["user"])
        mod_rank = access_ranks.get("mod", BASE_ACCESS_RANKS["mod"])
        admin_rank = access_ranks.get("admin", BASE_ACCESS_RANKS["admin"])

        if viewer_rank < mod_rank:
            query += " AND storage_history.user_id = ?"
            values.append(viewer_user_id)
        elif viewer_rank < admin_rank:
            allowed_levels = [level for level, rank in access_ranks.items() if rank <= viewer_rank]
            placeholders = ", ".join("?" for _ in allowed_levels)
            query += f" AND (storage_history.access_level IN ({placeholders}) OR storage_history.access_level IS NULL)"
            values.extend(allowed_levels)

        query += " ORDER BY storage_history.created_at DESC, storage_history.id DESC LIMIT 1"

        with self.connect() as connection:
            row = connection.execute(query, tuple(values)).fetchone()
        if row is None:
            return None

        return {
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "user_id": int(row["user_id"]) if row["user_id"] is not None else None,
            "access_level": str(row["access_level"]) if row["access_level"] is not None else "unknown",
            "source": str(row["source"]),
            "delta": int(row["delta"]),
            "details": json.loads(str(row["metadata"] or "{}")),
            "clan_name": str(row["clan_name"]),
            "item_name": str(row["item_name"]),
            "category": str(row["category"]),
        }

    def undo_latest_storage_entry(
        self,
        guild_id: int,
        *,
        viewer_access_level: str,
        viewer_user_id: int,
        clan_name: str,
        visible_clans: list[str] | None = None,
        actor_user_id: int | None = None,
        actor_access_level: str | None = None,
    ) -> dict[str, object] | None:
        latest_entry = self.get_latest_undoable_storage_entry(
            guild_id,
            viewer_access_level=viewer_access_level,
            viewer_user_id=viewer_user_id,
            clan_name=clan_name,
            visible_clans=visible_clans,
        )
        if latest_entry is None:
            return None

        next_quantity = self.adjust_storage(
            guild_id,
            str(latest_entry["clan_name"]),
            str(latest_entry["category"]),
            str(latest_entry["item_name"]),
            -int(latest_entry["delta"]),
            source="audit_undo_last",
            metadata={
                "undid_history_id": int(latest_entry["id"]),
                "undid_source": str(latest_entry["source"]),
                "undid_delta": int(latest_entry["delta"]),
            },
            user_id=actor_user_id,
            access_level=actor_access_level,
        )
        latest_entry["new_quantity"] = next_quantity
        return latest_entry

    def get_storage(self, guild_id: int, clan_name: str, category: str | None = None) -> dict[str, int]:
        clan = self.require_clan(guild_id, clan_name)
        query = """
            SELECT items.name, clan_storage.quantity
            FROM clan_storage
            JOIN items ON items.id = clan_storage.item_id
            WHERE clan_storage.clan_id = ?
        """
        values: list[object] = [clan.id]
        if category:
            query += " AND items.category = ?"
            values.append(category)
        query += " ORDER BY items.name"
        with self.connect() as connection:
            rows = connection.execute(query, tuple(values)).fetchall()
        return {row["name"]: int(row["quantity"]) for row in rows if int(row["quantity"]) > 0}

    def get_category_totals(self, guild_id: int, clan_name: str) -> dict[str, int]:
        clan = self.require_clan(guild_id, clan_name)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT items.category, SUM(clan_storage.quantity) AS total
                FROM clan_storage
                JOIN items ON items.id = clan_storage.item_id
                WHERE clan_storage.clan_id = ?
                GROUP BY items.category
                """,
                (clan.id,),
            ).fetchall()
        totals = defaultdict(int)
        for row in rows:
            totals[row["category"]] = int(row["total"] or 0)
        return dict(totals)

    def get_access_level_ranks(self, guild_id: int) -> dict[str, int]:
        levels = dict(BASE_ACCESS_RANKS)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name, rank FROM access_levels WHERE guild_id = ? ORDER BY rank, name",
                (guild_id,),
            ).fetchall()
        for row in rows:
            levels[str(row["name"])] = int(row["rank"])
        return levels

    def list_access_levels(self, guild_id: int) -> list[tuple[str, int, bool]]:
        levels = self.get_access_level_ranks(guild_id)
        return [
            (name, rank, name in BASE_ACCESS_RANKS)
            for name, rank in sorted(levels.items(), key=lambda pair: (pair[1], pair[0]))
        ]

    def create_access_level(self, guild_id: int, name: str, rank: int) -> str:
        normalized = self._normalize_access_level_name(name)
        if normalized in BASE_ACCESS_RANKS:
            raise ValueError(f"Cannot create reserved access level: {normalized}")
        if rank < 1 or rank >= BASE_ACCESS_RANKS["admin"]:
            raise ValueError("Rank must be between 1 and 299")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO access_levels (guild_id, name, rank)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, name) DO UPDATE SET rank = excluded.rank
                """,
                (guild_id, normalized, rank),
            )
        return normalized

    def remove_access_level(self, guild_id: int, name: str) -> bool:
        normalized = self._normalize_access_level_name(name)
        if normalized in BASE_ACCESS_RANKS:
            raise ValueError(f"Cannot remove built-in access level: {normalized}")
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM access_levels WHERE guild_id = ? AND name = ?",
                (guild_id, normalized),
            )
            removed = cursor.rowcount > 0
            if removed:
                connection.execute(
                    "DELETE FROM role_permissions WHERE guild_id = ? AND access_level = ?",
                    (guild_id, normalized),
                )
                connection.execute(
                    "DELETE FROM user_permissions WHERE guild_id = ? AND access_level = ?",
                    (guild_id, normalized),
                )
                connection.execute(
                    "DELETE FROM command_access_overrides WHERE guild_id = ? AND access_level = ?",
                    (guild_id, normalized),
                )
                connection.execute(
                    "DELETE FROM access_impersonation WHERE guild_id = ? AND access_level = ?",
                    (guild_id, normalized),
                )
        return removed

    def set_role_permission(self, guild_id: int, access_level: str, role_id: int) -> None:
        normalized = self._validate_access_level(guild_id, access_level)
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO role_permissions (guild_id, access_level, role_id) VALUES (?, ?, ?)",
                (guild_id, normalized, role_id),
            )

    def clear_role_permissions(self, guild_id: int, access_level: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM role_permissions WHERE guild_id = ? AND access_level = ?",
                (guild_id, access_level),
            )

    def get_role_permissions(self, guild_id: int) -> dict[str, list[int]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT access_level, role_id FROM role_permissions WHERE guild_id = ? ORDER BY access_level, role_id",
                (guild_id,),
            ).fetchall()
        permissions: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            permissions[row["access_level"]].append(int(row["role_id"]))
        return dict(permissions)

    def set_user_permission(self, guild_id: int, access_level: str, user_id: int) -> None:
        normalized = self._validate_access_level(guild_id, access_level)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO user_permissions (guild_id, user_id, access_level)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET access_level = excluded.access_level
                """,
                (guild_id, user_id, normalized),
            )

    def get_user_permission(self, guild_id: int, user_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT access_level FROM user_permissions WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        if row is None:
            return None
        access_level = str(row["access_level"])
        if access_level not in self.get_access_level_ranks(guild_id):
            return None
        return access_level

    def get_user_permissions(self, guild_id: int) -> dict[str, list[int]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT access_level, user_id FROM user_permissions WHERE guild_id = ? ORDER BY access_level, user_id",
                (guild_id,),
            ).fetchall()
        permissions: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            permissions[str(row["access_level"])].append(int(row["user_id"]))
        return dict(permissions)

    def set_command_access_override(self, guild_id: int, command_name: str, access_level: str) -> None:
        normalized = self._validate_access_level(guild_id, access_level)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO command_access_overrides (guild_id, command_name, access_level)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, command_name) DO UPDATE SET access_level = excluded.access_level
                """,
                (guild_id, command_name, normalized),
            )

    def clear_command_access_override(self, guild_id: int, command_name: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM command_access_overrides WHERE guild_id = ? AND command_name = ?",
                (guild_id, command_name),
            )
            return cursor.rowcount > 0

    def get_command_access_overrides(self, guild_id: int) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT command_name, access_level
                FROM command_access_overrides
                WHERE guild_id = ?
                ORDER BY command_name
                """,
                (guild_id,),
            ).fetchall()
        return {str(row["command_name"]): str(row["access_level"]) for row in rows}

    def get_command_access_level(
        self,
        guild_id: int,
        command_name: str,
        default_access: dict[str, str],
    ) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT access_level FROM command_access_overrides WHERE guild_id = ? AND command_name = ?",
                (guild_id, command_name),
            ).fetchone()

        if row:
            access_level = str(row["access_level"])
            if access_level in self.get_access_level_ranks(guild_id):
                return access_level
        return default_access.get(command_name, "user")

    def set_access_impersonation(self, guild_id: int, user_id: int, access_level: str) -> None:
        normalized = self._validate_access_level(guild_id, access_level)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO access_impersonation (guild_id, user_id, access_level)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET access_level = excluded.access_level
                """,
                (guild_id, user_id, normalized),
            )

    def clear_access_impersonation(self, guild_id: int, user_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM access_impersonation WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            return cursor.rowcount > 0

    def get_access_impersonation(self, guild_id: int, user_id: int) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT access_level FROM access_impersonation WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        if row is None:
            return None
        access_level = str(row["access_level"])
        if access_level not in self.get_access_level_ranks(guild_id):
            return None
        return access_level

    def require_clan(self, guild_id: int, name: str) -> Clan:
        clan = self.get_clan(guild_id, name)
        if clan is None:
            raise ValueError(f"Unknown clan: {name}")
        return clan

    def require_item(self, guild_id: int, category: str, name: str) -> Item:
        normalized_category = category.lower().strip()
        item = self.get_item(guild_id, normalized_category, name)
        if item is None:
            raise ValueError(f"Unknown item '{name.lower()}' in category '{normalized_category}'")
        return item

    @staticmethod
    def _row_to_clan(row: sqlite3.Row) -> Clan:
        return Clan(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            name=str(row["name"]),
            tracking_mode=str(row["tracking_mode"]),
            tracking_link=row["tracking_link"],
            cat_count=int(row["cat_count"]),
            alert_channel_id=int(row["alert_channel_id"]) if row["alert_channel_id"] is not None else None,
            alerts_enabled=bool(row["alerts_enabled"]),
            seasonal_modifier=float(row["seasonal_modifier"]) if "seasonal_modifier" in row.keys() else 1.0,
            roll_log_channel_id=int(row["roll_log_channel_id"])
            if "roll_log_channel_id" in row.keys() and row["roll_log_channel_id"] is not None
            else None,
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> Item:
        return Item(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            name=str(row["name"]),
            category=str(row["category"]),
            enabled=bool(row["enabled"]),
            is_default=bool(row["is_default"]),
            required_stat=int(row["required_stat"]) if "required_stat" in row.keys() and row["required_stat"] is not None else None,
            required_stat_name=str(row["required_stat_name"])
            if "required_stat_name" in row.keys() and row["required_stat_name"] is not None
            else None,
        )


COMMAND_ACCESS = {
    "test_seed_defaults": "admin",
    "test_seed_demo": "admin",
    "clan_create": "admin",
    "clan_config": "admin",
    "clan_delete": "admin",
    "clan_member_add": "mod",
    "clan_member_remove": "mod",
    "clan_member_show": "mod",
    "category_create": "admin",
    "category_list": "user",
    "category_threshold_set": "admin",
    "category_territory_rule_set": "admin",
    "category_remove_request": "admin",
    "category_remove_confirm": "admin",
    "item_add": "admin",
    "item_edit": "admin",
    "clan_item_link": "mod",
    "item_remove": "admin",
    "storage_add": "mod",
    "storage_set": "mod",
    "territory_create": "mod",
    "territory_item_set": "mod",
    "territory_item_set_bulk": "mod",
    "territory_item_seasonal_set": "mod",
    "territory_item_remove": "mod",
    "seasonal_modifier_set": "admin",
    "weight_breakdown": "user",
    "territory_link_set": "mod",
    "territory_link_show": "mod",
    "permission_set": "admin",
    "command_access_set": "admin",
    "command_access_reset": "admin",
    "command_access_show": "admin",
    "access_level_create": "admin",
    "access_level_remove": "admin",
    "access_level_list": "admin",
    "alert_config": "admin",
    "roll_forage": "user",
    "test_roll_forage": "user",
    "roll_config_show": "mod",
    "roll_config_set": "mod",
    "roll_config_reset": "mod",
    "preview_thread_link": "mod",
    "preview_forum_count": "mod",
    "territory_link_validate": "mod",
    "system_check": "mod",
    "linkage_show": "user",
    "import_csv_examples": "mod",
    "import": "mod",
    "preview_message_link": "mod",
    "storage_show": "user",
    "roll": "user",
    "audit_log_show": "user",
    "audit_undo_last": "user",
    "audit_export_json": "admin",
    "audit_clear": "admin",
    "dashboard_show": "user",
    "config_show": "user",
    "use_item": "user",
    "char_stat_set": "user",
    "char_stat_show": "user",
    "preyherb_help": "user",
    "quick_start": "user",
    "impersonate_access": "mod",
}
