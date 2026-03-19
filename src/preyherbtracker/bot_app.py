from __future__ import annotations

import re
import csv
import io
import json
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from preyherbtracker.config import Settings
from preyherbtracker.database import COMMAND_ACCESS, Database
from preyherbtracker.models import DEFAULT_HERBS, DEFAULT_PREY, TrackingMode
from preyherbtracker.rolling import RollResult, RollService
from preyherbtracker.tracking import (
    fetch_tracking_bytes,
    parse_spreadsheet_rows_from_bytes,
    parse_tracking_cat_count,
    parse_tracking_cat_count_from_bytes,
)


CategoryLiteral = str
TrackingLiteral = Literal["off", "manual", "forum", "spreadsheet"]


class PreyHerbTrackerBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.database = Database(settings.database_path)
        self.roll_service = RollService(self.database)

    async def setup_hook(self) -> None:
        self.database.initialize()
        if self.settings.bot_guild_id:
            guild = discord.Object(id=self.settings.bot_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} ({self.user.id})")


def access_rank(database: Database, guild_id: int, access_level: str) -> int:
    return database.get_access_level_ranks(guild_id).get(access_level, 0)


def access_allows(database: Database, guild_id: int, granted_level: str, required_level: str) -> bool:
    return access_rank(database, guild_id, granted_level) >= access_rank(database, guild_id, required_level)


def clean_message(title: str, lines: list[str] | None = None) -> str:
    parts = [f"**{title}**"]
    if lines:
        parts.extend(f"- {line}" for line in lines)
    return "\n".join(parts)


def clean_embed(title: str, lines: list[str] | None = None) -> discord.Embed:
    description = "\n".join(f"- {line}" for line in (lines or []))
    embed = discord.Embed(title=title, description=description or None, color=discord.Color.blurple())
    return embed


def chunk_lines_for_embed(title: str, lines: list[str], max_length: int = 3500) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    current_lines: list[str] = []
    current_length = 0
    for line in lines:
        rendered = f"- {line}\n"
        if current_lines and current_length + len(rendered) > max_length:
            embeds.append(clean_embed(title, current_lines))
            current_lines = [line]
            current_length = len(rendered)
        else:
            current_lines.append(line)
            current_length += len(rendered)
    if current_lines:
        embeds.append(clean_embed(title, current_lines))
    return embeds


def build_clan_dashboard_embed(clan, totals: dict[str, int], *, title_prefix: str = "Dashboard") -> discord.Embed:
    grand_total = sum(int(value) for value in totals.values())
    embed = discord.Embed(
        title=f"{title_prefix}: {clan.name}",
        color=discord.Color.green() if clan.alerts_enabled else discord.Color.blurple(),
    )
    embed.add_field(name="Tracking", value=f"`{clan.tracking_mode}`", inline=True)
    embed.add_field(name="Cats", value=f"`{clan.cat_count}`", inline=True)
    embed.add_field(name="Alerts", value=f"`{clan.alerts_enabled}`", inline=True)
    embed.add_field(name="Total Items", value=f"`{grand_total}`", inline=True)
    category_lines = [f"`{name}`: `{count}`" for name, count in sorted(totals.items())] or ["none"]
    embed.add_field(name="Categories", value="\n".join(category_lines[:10]), inline=False)
    if clan.cat_count > 0 and totals:
        ratio_lines = [f"`{name}` ratio: `{count / clan.cat_count:.2f}`" for name, count in sorted(totals.items())]
        embed.add_field(name="Ratios", value="\n".join(ratio_lines[:10]), inline=False)
    if clan.tracking_link:
        embed.add_field(name="Tracking Link", value=clan.tracking_link, inline=False)
    return embed


async def record_command_event(
    interaction: discord.Interaction,
    command_name: str,
    *,
    clan_name: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    if interaction.guild is None:
        return
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    access_level = get_member_access_level(interaction, bot.database)
    payload = dict(details or {})
    payload.setdefault(
        "command_link",
        f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel_id}/{interaction.id}",
    )
    bot.database.log_command_event(
        interaction.guild.id,
        command_name,
        user_id=interaction.user.id,
        access_level=access_level,
        clan_name=clan_name,
        details=payload,
    )


def format_audit_entry(entry: dict[str, object], guild: discord.Guild) -> str:
    timestamp = str(entry["created_at"])
    user_id = entry.get("user_id")
    actor = f"<@{user_id}>" if user_id is not None else "unknown user"
    access_level = str(entry.get("access_level") or "unknown")
    if entry["kind"] == "storage":
        delta = int(entry["delta"])
        delta_text = f"+{delta}" if delta >= 0 else str(delta)
        clan_name = str(entry["clan_name"])
        item_name = str(entry["item_name"])
        category = str(entry["category"])
        source = str(entry["source"])
        details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
        source_link = details.get("roll_message_link") if isinstance(details, dict) else None
        link_text = f" {source_link}" if isinstance(source_link, str) and source_link else ""
        return f"`{timestamp}` `{access_level}` {actor} {source} `{clan_name}` `{item_name}` ({category}) {delta_text}{link_text}"

    command_name = str(entry["command_name"])
    clan_name = entry.get("clan_name")
    clan_text = f" clan `{clan_name}`" if clan_name else ""
    details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
    command_link = details.get("command_link") if isinstance(details, dict) else None
    link_text = f" {command_link}" if isinstance(command_link, str) and command_link else ""
    return f"`{timestamp}` `{access_level}` {actor} ran `/{command_name}`{clan_text}{link_text}"


def get_base_member_access_level(interaction: discord.Interaction, database: Database) -> str:
    if interaction.guild is None:
        return "user"
    if interaction.user.id == interaction.guild.owner_id:
        return "admin"

    interaction_perms = interaction.permissions
    if interaction_perms.administrator:
        return "admin"

    guild_id = interaction.guild.id
    ranks = database.get_access_level_ranks(guild_id)
    resolved = "mod" if interaction_perms.manage_guild else "user"
    resolved_rank = ranks.get(resolved, 0)

    explicit_user_level = database.get_user_permission(guild_id, interaction.user.id)
    if explicit_user_level is not None:
        explicit_rank = ranks.get(explicit_user_level, 0)
        if explicit_rank > resolved_rank:
            resolved = explicit_user_level
            resolved_rank = explicit_rank

    role_map = database.get_role_permissions(guild_id)
    member_role_ids: set[int] = set()
    if isinstance(interaction.user, discord.Member):
        member_role_ids = {role.id for role in interaction.user.roles}
    for role_level, role_ids in role_map.items():
        if any(role_id in member_role_ids for role_id in role_ids):
            role_rank = ranks.get(role_level, 0)
            if role_rank > resolved_rank:
                resolved = role_level
                resolved_rank = role_rank
    return resolved


def get_member_access_level(interaction: discord.Interaction, database: Database) -> str:
    base_level = get_base_member_access_level(interaction, database)
    if interaction.guild is None:
        return base_level

    impersonated_level = database.get_access_impersonation(interaction.guild.id, interaction.user.id)
    if impersonated_level is None:
        return base_level
    if access_allows(database, interaction.guild.id, base_level, impersonated_level):
        return impersonated_level
    return base_level


def can_manage_all_clans(guild_id: int, database: Database, access_level: str) -> bool:
    return access_allows(database, guild_id, access_level, "mod")


def get_visible_clan_names(interaction: discord.Interaction, database: Database) -> list[str]:
    if interaction.guild is None:
        return []

    access_level = get_member_access_level(interaction, database)
    if can_manage_all_clans(interaction.guild.id, database, access_level):
        return [clan.name for clan in database.list_clans(interaction.guild.id)]
    return database.list_member_clans(interaction.guild.id, interaction.user.id)


async def require_clan_write_access(
    interaction: discord.Interaction,
    database: Database,
    clan_name: str,
) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return False

    access_level = get_member_access_level(interaction, database)
    if can_manage_all_clans(interaction.guild.id, database, access_level):
        return True
    if database.user_has_clan_membership(interaction.guild.id, interaction.user.id, clan_name):
        return True

    embed = clean_embed(
        "Clan Access Required",
        [
            f"You are not assigned to `{clan_name.lower()}`.",
            "Admins and mods can manage any clan.",
            "Lower access levels must be linked to a clan first.",
        ],
    )
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


async def require_access(interaction: discord.Interaction, database: Database, command_name: str) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return False

    required_level = database.get_command_access_level(interaction.guild.id, command_name, COMMAND_ACCESS)
    if required_level == "user":
        return True

    if command_name == "impersonate_access":
        user_level = get_base_member_access_level(interaction, database)
    else:
        user_level = get_member_access_level(interaction, database)
    if access_allows(database, interaction.guild.id, user_level, required_level):
        return True

    if interaction.response.is_done():
        await interaction.followup.send(embed=clean_embed("Access Denied", ["You do not have permission for this command."]), ephemeral=True)
    else:
        await interaction.response.send_message(embed=clean_embed("Access Denied", ["You do not have permission for this command."]), ephemeral=True)
    return False


async def maybe_send_ratio_alert(bot: PreyHerbTrackerBot, guild: discord.Guild, clan_name: str) -> None:
    clan = bot.database.require_clan(guild.id, clan_name)
    if not clan.alerts_enabled or not clan.alert_channel_id:
        return

    totals = bot.database.get_category_totals(guild.id, clan_name)
    thresholds = bot.database.get_clan_category_alert_thresholds(guild.id, clan_name)
    warnings: list[str] = []
    for category_name, config in thresholds.items():
        total = totals.get(category_name, 0)
        if config.mode == "static":
            if total < config.value:
                warnings.append(f"{category_name} storage is low ({total}/{config.value:.0f} items)")
        else:  # dynamic
            if clan.cat_count <= 0:
                continue
            ratio = total / clan.cat_count
            if ratio < config.value:
                warnings.append(f"{category_name} ratio is low ({ratio:.2f}/{config.value:.2f} per cat)")
    if not warnings:
        return

    channel = guild.get_channel(clan.alert_channel_id)
    if channel and isinstance(channel, discord.abc.Messageable):
        await channel.send(f"Alert for **{clan.name}**: " + "; ".join(warnings))


def format_forage_result(result: RollResult, *, dry_run: bool = False) -> discord.Embed:
    territory_text = result.territory_name or "default pool"
    finds_text = ", ".join(result.finds) if result.finds else "nothing"
    if dry_run:
        storage_line = "Dry run only: nothing was stored."
    elif result.stored and result.clan_name:
        storage_line = f"Stored in `{result.clan_name.lower()}`."
    else:
        storage_line = "No clan provided, so nothing was stored. You may use /storage_add to add you're catch (requires roll message link)."

    return clean_embed("Test Forage Result" if dry_run else "Forage Result",
        [
            f"Roll: d20 `{result.die_roll}` with modifier `{result.modifier:+d}` = total `{result.total}`.",
            f"Pool: `{territory_text}`.",
            f"Finds: `{finds_text}`.",
            storage_line,
        ],
    )


async def clan_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []

    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    current_lower = current.strip().lower()
    clan_names = get_visible_clan_names(interaction, bot.database)

    if not current_lower:
        return [app_commands.Choice(name=clan_name, value=clan_name) for clan_name in clan_names[:25]]

    matches = [clan_name for clan_name in clan_names if current_lower in clan_name]
    return [app_commands.Choice(name=clan_name, value=clan_name) for clan_name in matches[:25]]


async def territory_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    current_lower = current.strip().lower()
    names = bot.database.list_territory_names(interaction.guild.id)
    matches = [n for n in names if current_lower in n] if current_lower else names
    return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


async def category_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    current_lower = current.strip().lower()
    categories = bot.database.list_categories(interaction.guild.id)
    matches = [name for name in categories if current_lower in name] if current_lower else categories
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


def parse_territory_names(raw_names: str) -> list[str]:
    names: list[str] = []
    for part in raw_names.replace("\n", ",").split(","):
        normalized = part.strip().lower()
        if normalized and normalized not in names:
            names.append(normalized)
    return names


def parse_channel_id_from_input(value: str) -> int | None:
    stripped = value.strip()
    if stripped.isdigit():
        return int(stripped)
    # For Discord URLs this may capture guild/channel/thread IDs; callers may need to resolve candidates.
    match = re.search(r"(\d{15,22})", stripped)
    if match is None:
        return None
    return int(match.group(1))


def parse_discord_message_link(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"discord\.com/channels/(\d{15,22})/(\d{15,22})/(\d{15,22})", value.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_discord_tracking_link(value: str) -> bool:
    normalized = value.strip()
    if normalized.isdigit():
        return True
    return re.search(
        r"(?:https?://)?(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/\d{15,22}/\d{15,22}(?:/\d{15,22})?",
        normalized,
        re.IGNORECASE,
    ) is not None


def extract_forum_catalog_entry(message: discord.Message) -> dict[str, object] | None:
    content = (message.content or "").strip()
    embed_desc = ""
    if message.embeds:
        first_embed = message.embeds[0]
        embed_desc = (first_embed.description or "").strip()
    text_blob = f"{content}\n{embed_desc}".strip()
    if not text_blob:
        return None

    first_line = text_blob.splitlines()[0].strip()
    name_candidate = first_line.split(" - ", 1)[0].split(":", 1)[0].strip().lower()
    if not name_candidate or len(name_candidate) > 64:
        return None

    territories_match = re.search(r"(?:territory|found in)\s*:\s*([^\n]+)", text_blob, re.IGNORECASE)
    clan_match = re.search(r"(?:clan|region)\s*:\s*([^\n]+)", text_blob, re.IGNORECASE)
    stat_match = re.search(r"(?:stat|req(?:uired)?\s*stat)\s*[:=]\s*(\d+)", text_blob, re.IGNORECASE)

    territories: list[str] = []
    if territories_match:
        territories = parse_territory_names(territories_match.group(1))

    clan_name = clan_match.group(1).strip().lower() if clan_match else None
    required_stat = int(stat_match.group(1)) if stat_match else None

    return {
        "name": name_candidate,
        "territories": territories,
        "clan": clan_name,
        "required_stat": required_stat,
    }


def _normalize_unicode_digits(value: str) -> str:
    unicode_digit_map = {
        "𝟎": "0",
        "𝟏": "1",
        "𝟐": "2",
        "𝟑": "3",
        "𝟒": "4",
        "𝟓": "5",
        "𝟔": "6",
        "𝟕": "7",
        "𝟖": "8",
        "𝟗": "9",
    }
    for unicode_char, ascii_digit in unicode_digit_map.items():
        value = value.replace(unicode_char, ascii_digit)
    return value


def extract_thread_catalog_entry(message: discord.Message) -> dict[str, object] | None:
    content = (message.content or "").strip()
    if not content:
        return None

    first_line = content.splitlines()[0]
    marker_match = re.search(r"➺・([^―]+)―(.+)", first_line)
    if marker_match is None:
        return None

    name_raw = marker_match.group(1).strip().lower()
    if not name_raw or len(name_raw) > 64:
        return None

    stat_section = marker_match.group(2).strip()
    stat_match = re.search(r"\+([\d𝟎-𝟗]+)", stat_section)
    required_stat = None
    if stat_match:
        stat_value_str = _normalize_unicode_digits(stat_match.group(1))
        try:
            required_stat = int(stat_value_str)
        except ValueError:
            pass

    return {
        "name": name_raw,
        "territories": [],
        "clan": None,
        "required_stat": required_stat,
    }


async def resolve_forum_channel_from_link(guild: discord.Guild, tracking_link: str) -> discord.ForumChannel:
    id_candidates = [int(value) for value in re.findall(r"(\d{15,22})", tracking_link)]
    if tracking_link.strip().isdigit():
        id_candidates = [int(tracking_link.strip())]
    if not id_candidates:
        raise ValueError("Tracking link does not contain a Discord channel or thread ID")

    for candidate in reversed(id_candidates):
        channel = guild.get_channel(candidate)
        if isinstance(channel, discord.ForumChannel):
            return channel
        if isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.ForumChannel):
            return channel.parent

        thread = guild.get_thread(candidate)
        if thread is not None and isinstance(thread.parent, discord.ForumChannel):
            return thread.parent

        try:
            fetched = await guild.fetch_channel(candidate)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

        if isinstance(fetched, discord.ForumChannel):
            return fetched
        if isinstance(fetched, discord.Thread) and isinstance(fetched.parent, discord.ForumChannel):
            return fetched.parent

    raise ValueError("Could not resolve a forum channel from tracking link")


async def resolve_thread_from_link(guild: discord.Guild, thread_link: str) -> discord.Thread:
    id_candidates = [int(value) for value in re.findall(r"(\d{15,22})", thread_link)]
    if thread_link.strip().isdigit():
        id_candidates = [int(thread_link.strip())]
    if not id_candidates:
        raise ValueError("Thread link does not contain a Discord thread ID")

    for candidate in reversed(id_candidates):
        channel = guild.get_channel(candidate)
        if isinstance(channel, discord.Thread):
            return channel

        thread = guild.get_thread(candidate)
        if isinstance(thread, discord.Thread):
            return thread

        try:
            fetched = await guild.fetch_channel(candidate)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            continue

        if isinstance(fetched, discord.Thread):
            return fetched

    raise ValueError("Could not resolve a thread from link")


async def resolve_message_from_link(
    guild: discord.Guild,
    message_link: str,
) -> tuple[discord.abc.GuildChannel | discord.Thread, discord.Message]:
    parsed_link = parse_discord_message_link(message_link)
    if parsed_link is None:
        raise ValueError("Provide a valid Discord message link")

    link_guild_id, channel_id, message_id = parsed_link
    if guild.id != link_guild_id:
        raise ValueError("Message link points to a different server")

    target_channel = guild.get_channel(channel_id)
    if target_channel is None:
        maybe_thread = guild.get_thread(channel_id)
        if isinstance(maybe_thread, discord.Thread):
            target_channel = maybe_thread

    if target_channel is None:
        try:
            fetched_channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            raise ValueError("Could not access the linked channel or thread") from exc
        if not isinstance(fetched_channel, discord.Thread | discord.abc.GuildChannel):
            raise ValueError("Resolved link target is not a server channel or thread")
        target_channel = fetched_channel

    if not hasattr(target_channel, "fetch_message"):
        raise ValueError("Resolved link target does not support message lookup")

    try:
        message = await target_channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        raise ValueError("Could not fetch the linked message") from exc

    return target_channel, message


def summarize_message_text(value: str | None, *, max_length: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", (value or "").strip())
    if not normalized:
        return "(empty)"
    if len(normalized) <= max_length:
        return normalized
    return f"{normalized[: max_length - 3]}..."


async def count_forum_threads_breakdown(forum_channel: discord.ForumChannel) -> tuple[int, int, bool]:
    active_thread_ids: set[int] = {
        thread.id
        for thread in forum_channel.threads
        if thread.parent_id == forum_channel.id
    }
    try:
        for thread in await forum_channel.guild.active_threads():
            if thread.parent_id == forum_channel.id:
                active_thread_ids.add(thread.id)
    except (discord.Forbidden, discord.HTTPException):
        pass

    archived_count = 0
    archived_complete = True
    try:
        async for _thread in forum_channel.archived_threads(limit=None):
            archived_count += 1
    except (discord.Forbidden, discord.HTTPException):
        archived_complete = False
    return len(active_thread_ids), archived_count, archived_complete


async def count_forum_threads(forum_channel: discord.ForumChannel) -> int:
    active_count, archived_count, _ = await count_forum_threads_breakdown(forum_channel)
    return active_count + archived_count


def parse_roll_ranges_input(raw_ranges: str) -> list[tuple[int, int, int]]:
    """Parses `min-max:dose` pairs separated by commas."""
    ranges: list[tuple[int, int, int]] = []
    for chunk in raw_ranges.split(","):
        part = chunk.strip()
        if not part:
            continue
        if ":" not in part or "-" not in part:
            raise ValueError("Each range must be in `min-max:dose` format")
        bounds_text, dose_text = [item.strip() for item in part.split(":", 1)]
        min_text, max_text = [item.strip() for item in bounds_text.split("-", 1)]
        min_total = int(min_text)
        max_total = int(max_text)
        dose = int(dose_text)
        ranges.append((min_total, max_total, dose))
    if not ranges:
        raise ValueError("Provide at least one range")
    return ranges


def parse_csv_rows(csv_text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[dict[str, str]] = []
    for row in reader:
        normalized: dict[str, str] = {}
        for key, value in row.items():
            if key is None:
                continue
            normalized[str(key).strip().lower()] = str(value or "").strip()
        if normalized:
            rows.append(normalized)
    if not rows:
        raise ValueError("CSV contains no data rows")
    return rows


def process_catalog_import_row(
    bot: PreyHerbTrackerBot,
    guild_id: int,
    target: Literal["territories", "items", "links"],
    row: dict[str, str],
    *,
    apply_changes: bool,
) -> str:
    if target == "territories":
        name = row.get("name", "").lower()
        clan_name = row.get("clan") or None
        channel_id_text = row.get("channel_id", "")
        channel_id = int(channel_id_text) if channel_id_text.isdigit() else None
        if not name:
            raise ValueError("missing `name`")
        exists = bot.database.get_territory(guild_id, name) is not None
        if apply_changes:
            if not exists:
                bot.database.create_territory(guild_id, name, clan_name, channel_id=channel_id)
            elif channel_id is not None:
                bot.database.set_territory_channel_link(guild_id, name, channel_id)
        if exists:
            return f"territory `{name}` update"
        return f"territory `{name}` create"

    if target == "items":
        name = row.get("name", "").lower()
        category = row.get("category", "").lower()
        if not name:
            raise ValueError("missing `name`")
        if not category:
            raise ValueError("missing `category`")
        bot.database.require_category(guild_id, category)
        enabled_value = (row.get("enabled", "true") or "true").strip().lower()
        enabled = enabled_value in {"1", "true", "yes", "y"}
        required_stat_value = (row.get("required_stat", "") or "").strip()
        required_stat = int(required_stat_value) if required_stat_value else None
        required_stat_name = (row.get("required_stat_name", "") or "").strip() or None
        exists = bot.database.get_item(guild_id, category, name) is not None
        if apply_changes:
            bot.database.add_item(
                guild_id,
                name,
                category,
                enabled=enabled,
                required_stat=required_stat,
                required_stat_name=required_stat_name,
            )
        if exists:
            return f"item `{name}` ({category}) update"
        return f"item `{name}` ({category}) create"

    territory = row.get("territory", "").lower()
    category = row.get("category", "").lower()
    item_name = row.get("item_name", "").lower()
    weight = float(row.get("weight", "0") or 0)
    seasonal_modifier = float(row.get("seasonal_modifier", "1") or 1)
    if not territory or not category or not item_name:
        raise ValueError("missing one of territory/category/item_name")
    bot.database.require_category(guild_id, category)
    existing = [entry for entry in bot.database.list_territory_items(guild_id, territory, category) if entry.item_name == item_name]
    if apply_changes:
        bot.database.set_territory_item_weight(
            guild_id,
            territory,
            category,
            item_name,
            weight,
            seasonal_modifier=seasonal_modifier,
        )
    if existing:
        return f"link `{territory}` `{category}` `{item_name}` update"
    return f"link `{territory}` `{category}` `{item_name}` create"


def build_linkage_validation_report(bot: PreyHerbTrackerBot, guild_id: int) -> list[str]:
    issues: list[str] = []
    territories = bot.database.list_territory_channel_links(guild_id)
    links = bot.database.list_all_territory_item_links(guild_id)
    enabled_items = bot.database.list_items(guild_id, enabled_only=True)

    if not territories:
        issues.append("No territories are configured.")

    territory_names = {name for name, _ in territories}
    territory_with_links = {str(entry["territory_name"]) for entry in links}
    for name, channel_id in territories:
        if channel_id is None:
            issues.append(f"Territory `{name}` has no linked channel/thread.")
        if name not in territory_with_links:
            issues.append(f"Territory `{name}` has no item links.")

    linked_items = {(str(entry["category"]), str(entry["item_name"])) for entry in links}
    for item in enabled_items:
        key = (item.category, item.name)
        if key not in linked_items and territory_names:
            issues.append(f"Enabled `{item.category}` item `{item.name}` is not linked to any territory.")

    for category in bot.database.list_categories(guild_id):
        if not any(item.category == category for item in enabled_items):
            issues.append(f"No enabled `{category}` items are configured.")

    return issues


def resolve_territory_from_context(
    interaction: discord.Interaction,
    database: Database,
    *,
    allow_name_fallback: bool = True,
) -> str | None:
    guild = interaction.guild
    if guild is None:
        return None

    channel_ids: list[int] = []
    channel_names: list[str] = []
    channel = interaction.channel

    if channel is not None and hasattr(channel, "id"):
        channel_id = getattr(channel, "id", None)
        if isinstance(channel_id, int):
            channel_ids.append(channel_id)

    if isinstance(channel, discord.Thread):
        channel_names.append(channel.name)
        if channel.parent_id is not None:
            channel_ids.append(channel.parent_id)
        if channel.parent is not None:
            channel_names.append(channel.parent.name)
        if channel.category is not None:
            channel_names.append(channel.category.name)
    elif isinstance(channel, discord.abc.GuildChannel):
        channel_names.append(channel.name)
        if channel.category_id is not None:
            channel_ids.append(channel.category_id)
        if channel.category is not None:
            channel_names.append(channel.category.name)

    # Keep insertion order while removing duplicates.
    dedup_ids = list(dict.fromkeys(channel_ids))
    dedup_names = list(dict.fromkeys(name for name in channel_names if name))
    return database.detect_territory_from_channels(
        guild.id,
        dedup_ids,
        dedup_names,
        allow_name_fallback=allow_name_fallback,
    )


async def item_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """Returns item names, optionally filtered by the category param already typed."""
    if interaction.guild is None:
        return []
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    # Try to read the category sibling parameter from the interaction namespace.
    namespace = interaction.namespace
    category: str | None = getattr(namespace, "category", None)
    current_lower = current.strip().lower()
    items = bot.database.list_items(interaction.guild.id, category, enabled_only=False)
    matches = [i for i in items if current_lower in i.name] if current_lower else items
    return [app_commands.Choice(name=i.name, value=i.name) for i in matches[:25]]


async def command_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current_lower = current.strip().lower()
    command_names = sorted(COMMAND_ACCESS.keys())
    matches = [command_name for command_name in command_names if current_lower in command_name] if current_lower else command_names
    return [app_commands.Choice(name=command_name, value=command_name) for command_name in matches[:25]]


@app_commands.command(name="test_seed_defaults", description="Seed default starter categories and items for this server.")
async def test_seed_defaults(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "test_seed_defaults"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "test_seed_defaults")
    bot.database.create_category(guild.id, "herb")
    bot.database.create_category(guild.id, "prey")
    for item_name, _ in DEFAULT_HERBS:
        bot.database.add_item(guild.id, item_name, "herb", is_default=True)
    for item_name, _ in DEFAULT_PREY:
        bot.database.add_item(guild.id, item_name, "prey", is_default=True)
    await interaction.response.send_message(
        embed=clean_embed(
            "Defaults Seeded",
            [
                "Added built-in starter items in default categories.",
                "This command does not create clans.",
                "Use `/clan_create` and `/clan_list` next.",
            ],
        )
    )


@app_commands.command(name="test_seed_demo", description="Seed starter categories/items and create a starter clan if missing.")
@app_commands.describe(
    clan_name="Name for the starter clan (default: birchclan)",
    tracking_mode="How the clan's cat count is tracked",
    cat_count="Starting number of cats in the clan",
)
async def test_seed_demo(
    interaction: discord.Interaction,
    clan_name: str = "birchclan",
    tracking_mode: TrackingLiteral = "manual",
    cat_count: app_commands.Range[int, 0, 500] = 12,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "test_seed_demo"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "test_seed_demo", clan_name=clan_name)

    bot.database.create_category(guild.id, "herb")
    bot.database.create_category(guild.id, "prey")
    for item_name, _ in DEFAULT_HERBS:
        bot.database.add_item(guild.id, item_name, "herb", is_default=True)
    for item_name, _ in DEFAULT_PREY:
        bot.database.add_item(guild.id, item_name, "prey", is_default=True)

    normalized_clan_name = clan_name.strip().lower()
    existing = bot.database.get_clan(guild.id, normalized_clan_name)
    if existing is None:
        clan = bot.database.create_clan(
            guild.id,
            normalized_clan_name,
            tracking_mode=tracking_mode,
            cat_count=cat_count,
        )
        await interaction.response.send_message(
            embed=clean_embed(
                "Demo Setup Complete",
                [
                    "Seeded default starter categories and items.",
                    f"Created clan `{clan.name}`.",
                    f"Tracking mode: `{clan.tracking_mode}`.",
                    f"Cat count: `{clan.cat_count}`.",
                ],
            )
        )
        return

    await interaction.response.send_message(
        embed=clean_embed(
            "Defaults Seeded",
            [
                "Added default starter categories and items.",
                f"Clan `{existing.name}` already exists and was not changed.",
            ],
        )
    )


@app_commands.command(name="clan_create", description="Create a new clan.")
@app_commands.describe(
    name="Unique name for the clan (e.g. birchclan)",
    tracking_mode="How the cat count is maintained: off, manual, forum, or spreadsheet",
    tracking_link="Forum channel/thread link or spreadsheet URL (required for forum/spreadsheet modes)",
    cat_count="Current number of cats; used to calculate category ratio alerts",
)
async def clan_create(
    interaction: discord.Interaction,
    name: str,
    tracking_mode: TrackingLiteral = "off",
    tracking_link: str | None = None,
    cat_count: app_commands.Range[int, 0, 500] = 0,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "clan_create"):
        return
    guild = interaction.guild
    assert guild is not None
    if bot.database.get_clan(guild.id, name.lower().strip()) is not None:
        await interaction.response.send_message(
            embed=clean_embed("Clan Already Exists", [
                f"A clan named `{name.lower().strip()}` already exists.",
                "Use `/clan_config` to update it.",
            ]),
            ephemeral=True,
        )
        return
    await record_command_event(interaction, "clan_create", clan_name=name)
    clan = bot.database.create_clan(guild.id, name, tracking_mode=tracking_mode, tracking_link=tracking_link, cat_count=cat_count)
    await interaction.response.send_message(
        embed=clean_embed(
            "Clan Created",
            [
                f"Name: `{clan.name}`.",
                f"Tracking mode: `{clan.tracking_mode}`.",
                f"Cat count: `{clan.cat_count}`.",
            ],
        )
    )


@app_commands.command(name="clan_list", description="List all clans in this server.")
async def clan_list(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "clan_list")
    clans = bot.database.list_clans(guild.id)
    if not clans:
        await interaction.response.send_message(embed=clean_embed("No Clans Yet", ["Use `/clan_create` to add your first clan."]))
        return
    lines = [f"`{clan.name}` - tracking `{clan.tracking_mode}` - cats `{clan.cat_count}`" for clan in clans]
    await interaction.response.send_message(embed=clean_embed("Clans", lines))


@app_commands.command(name="clan_config", description="Update a clan's tracking, alert, and name settings.")
@app_commands.describe(
    clan="Clan to update",
    new_name="Rename this clan; territory and storage data is preserved but commands must use the new name",
    tracking_mode="How the cat count is maintained",
    tracking_link="Forum channel/thread link or spreadsheet URL",
    cat_count="Current number of cats in this clan",
    alerts_enabled="Enable or disable storage alerts for this clan",
    alert_channel="Channel where low-storage alert messages are posted",
    roll_log_channel="Channel where this clan's roll outputs should be mirrored",
)
async def clan_config(
    interaction: discord.Interaction,
    clan: str,
    new_name: str | None = None,
    tracking_mode: TrackingLiteral | None = None,
    tracking_link: str | None = None,
    cat_count: app_commands.Range[int, 0, 500] | None = None,
    alerts_enabled: bool | None = None,
    alert_channel: discord.TextChannel | None = None,
    roll_log_channel: discord.TextChannel | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "clan_config"):
        return
    guild = interaction.guild
    assert guild is not None
    old_name = clan.lower().strip()
    name_changed = False
    if new_name is not None and new_name.lower().strip() != old_name:
        try:
            bot.database.rename_clan(guild.id, clan, new_name)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Rename Failed", [str(exc)]), ephemeral=True)
            return
        clan = new_name.lower().strip()
        name_changed = True
    await record_command_event(interaction, "clan_config", clan_name=clan)
    try:
        updated = bot.database.update_clan_config(
            guild.id,
            clan,
            tracking_mode=tracking_mode,
            tracking_link=tracking_link,
            cat_count=cat_count,
            alerts_enabled=alerts_enabled,
            alert_channel_id=alert_channel.id if alert_channel else None,
            roll_log_channel_id=roll_log_channel.id if roll_log_channel else None,
        )
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Config Failed", [str(exc)]), ephemeral=True)
        return
    if updated.tracking_mode == "forum" and updated.tracking_link and (tracking_mode is not None or tracking_link is not None):
        try:
            _forum_ch = await resolve_forum_channel_from_link(guild, updated.tracking_link)
            _new_count = await count_forum_threads(_forum_ch)
            updated = bot.database.update_clan_config(guild.id, updated.name, cat_count=_new_count)
        except Exception:
            pass
    lines = [
        f"Name: `{updated.name}`.",
        f"Tracking: `{updated.tracking_mode}`.",
        f"Cats: `{updated.cat_count}`.",
        f"Alerts enabled: `{updated.alerts_enabled}`.",
        f"Roll log channel: `{updated.roll_log_channel_id or 'unset'}`.",
    ]
    if name_changed:
        lines.append(
            f":warning: Clan renamed from `{old_name}` to `{updated.name}`. "
            "Storage, members, and territory data are preserved, but any territory links "
            "owned by this clan will no longer auto-detect until territory names are checked. "
            "All commands must now use the new name."
        )
    await interaction.response.send_message(embed=clean_embed("Clan Updated", lines))


@app_commands.command(name="clan_delete", description="Permanently delete a clan and all its storage, members, and territory links.")
@app_commands.describe(
    clan="Clan to delete",
    confirm="True to confirm deletion; False (default) previews what will be removed",
)
async def clan_delete(
    interaction: discord.Interaction,
    clan: str,
    confirm: bool = False,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "clan_delete"):
        return
    guild = interaction.guild
    assert guild is not None
    try:
        clan_obj = bot.database.require_clan(guild.id, clan)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Unknown Clan", [str(exc)]), ephemeral=True)
        return

    territory_names = bot.database.list_territory_names_by_clan(guild.id, clan_obj.name)
    member_ids = bot.database.list_clan_members(guild.id, clan_obj.name)
    storage_total = sum(bot.database.get_storage(guild.id, clan_obj.name).values())

    if not confirm:
        territory_preview = ", ".join(f"`{n}`" for n in territory_names[:5])
        if len(territory_names) > 5:
            territory_preview += f" … and {len(territory_names) - 5} more"
        lines = [
            f"Clan: `{clan_obj.name}`.",
            ":warning: This will **permanently** delete all clan data and cannot be undone.",
            f"Territories owned: `{len(territory_names)}`" + (f" — {territory_preview}" if territory_names else "") + ".",
            f"Clan members linked: `{len(member_ids)}`.",
            f"Total storage items: `{storage_total}`.",
            "Re-run with `confirm: True` to confirm deletion.",
        ]
        await interaction.response.send_message(embed=clean_embed("Clan Delete Preview", lines), ephemeral=True)
        return

    await record_command_event(interaction, "clan_delete", clan_name=clan_obj.name)
    bot.database.delete_clan(guild.id, clan_obj.name)
    lines = [
        f"Clan `{clan_obj.name}` has been permanently deleted.",
        f"Territories removed: `{len(territory_names)}`.",
        f"Members unlinked: `{len(member_ids)}`.",
        f"Storage items removed: `{storage_total}`.",
    ]
    await interaction.response.send_message(embed=clean_embed("Clan Deleted", lines), ephemeral=True)


@app_commands.command(name="clan_member_add", description="Assign a user to a clan for lower-tier storage access.")
@app_commands.describe(
    clan="Clan to assign the user to",
    member="Discord member to link to this clan",
)
async def clan_member_add(interaction: discord.Interaction, clan: str, member: discord.Member) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "clan_member_add"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "clan_member_add", clan_name=clan, details={"member_id": member.id})
    bot.database.add_clan_member(guild.id, clan, member.id)
    await interaction.response.send_message(
        embed=clean_embed("Clan Member Added", [f"{member.mention} can now manage `{clan.lower()}` as a lower-tier user."])
    )


@app_commands.command(name="clan_member_remove", description="Remove a user's clan assignment.")
@app_commands.describe(
    clan="Clan to remove the user from",
    member="Discord member to unlink from this clan",
)
async def clan_member_remove(interaction: discord.Interaction, clan: str, member: discord.Member) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "clan_member_remove"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "clan_member_remove", clan_name=clan, details={"member_id": member.id})
    removed = bot.database.remove_clan_member(guild.id, clan, member.id)
    if removed:
        await interaction.response.send_message(
            embed=clean_embed("Clan Member Removed", [f"Removed {member.mention} from `{clan.lower()}`."])
        )
    else:
        await interaction.response.send_message(
            embed=clean_embed("No Change", [f"{member.mention} was not assigned to `{clan.lower()}`."])
        )


@app_commands.command(name="clan_member_show", description="Show users assigned to a clan.")
@app_commands.describe(
    clan="Clan whose assigned members you want to view",
)
async def clan_member_show(interaction: discord.Interaction, clan: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "clan_member_show"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "clan_member_show", clan_name=clan)
    member_ids = bot.database.list_clan_members(guild.id, clan)
    if not member_ids:
        await interaction.response.send_message(embed=clean_embed("No Clan Members", [f"No users are assigned to `{clan.lower()}`."]))
        return

    lines: list[str] = []
    for member_id in member_ids:
        member = guild.get_member(member_id)
        if member is not None:
            lines.append(member.mention)
        else:
            lines.append(f"<@{member_id}>")
    await interaction.response.send_message(embed=clean_embed(f"Clan Members: {clan.lower()}", lines))


@app_commands.command(name="category_create", description="Create a new item category.")
@app_commands.describe(
    name="Category name (e.g. prey, herb, fish, insects)",
)
async def category_create(interaction: discord.Interaction, name: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "category_create"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "category_create", details={"category": name.lower()})
    try:
        category_name = bot.database.create_category(guild.id, name)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Category Error", [str(exc)]), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=clean_embed("Category Created", [f"Category `{category_name}` is now available for items and rolls."])
    )


@app_commands.command(name="category_list", description="List configured categories and optional clan thresholds.")
@app_commands.describe(
    clan="Optional clan name to also show that clan's alert thresholds",
)
async def category_list(interaction: discord.Interaction, clan: str | None = None) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "category_list", clan_name=clan)
    categories = bot.database.list_categories(guild.id)
    if not categories:
        await interaction.response.send_message(embed=clean_embed("No Categories", ["No categories exist yet."]))
        return
    lines = [f"`{name}`" for name in categories]
    if clan:
        try:
            thresholds = bot.database.get_clan_category_alert_thresholds(guild.id, clan)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Unknown Clan", [str(exc)]), ephemeral=True)
            return
        lines.append("")
        lines.append(f"Thresholds for `{clan.lower()}`:")
        for name, cfg in sorted(thresholds.items()):
            if cfg.mode == "static":
                lines.append(f"`{name}`: `{cfg.value:.0f}` fixed items")
            else:
                lines.append(f"`{name}`: `{cfg.value}` per cat")
    await interaction.response.send_message(embed=clean_embed("Categories", lines))


@app_commands.command(name="category_threshold_set", description="Set a clan alert threshold for one category.")
@app_commands.describe(
    clan="Clan whose threshold should be updated",
    category="Category to set the threshold for",
    mode="Threshold mode: dynamic (per-cat multiplier) or static (fixed item count)",
    threshold="Threshold value — for dynamic: per-cat ratio (e.g. 0.5); for static: total item count (e.g. 20)",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="dynamic (per-cat ratio)", value="dynamic"),
    app_commands.Choice(name="static (fixed item count)", value="static"),
])
async def category_threshold_set(
    interaction: discord.Interaction,
    clan: str,
    category: str,
    threshold: app_commands.Range[float, 0, 10000],
    mode: str = "dynamic",
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "category_threshold_set"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(
        interaction,
        "category_threshold_set",
        clan_name=clan,
        details={"category": category.lower(), "threshold": threshold, "mode": mode},
    )
    try:
        bot.database.set_clan_category_alert_threshold(guild.id, clan, category, threshold, mode=mode)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Threshold Update Failed", [str(exc)]), ephemeral=True)
        return
    mode_label = "per-cat ratio" if mode == "dynamic" else "fixed item count"
    await interaction.response.send_message(
        embed=clean_embed(
            "Category Threshold Updated",
            [
                f"Clan: `{clan.lower()}`.",
                f"Category: `{category.lower()}`.",
                f"Mode: `{mode}` ({mode_label}).",
                f"Threshold: `{threshold}`.",
            ],
        )
    )


@app_commands.command(name="category_territory_rule_set", description="Set territory-link or stat-input requirements globally, by category, or by item.")
@app_commands.describe(
    rule="Which rule to update: territory_link or stat_input",
    scope="Scope to update: global, category, or item",
    required="If true, rule is required at this scope",
    category="Required for category/item scopes",
    item_name="Required for item scope",
    force="When true for global/category, push this value down to lower scopes",
)
async def category_territory_rule_set(
    interaction: discord.Interaction,
    rule: Literal["territory_link", "stat_input"],
    scope: Literal["global", "category", "item"],
    required: bool,
    category: str | None = None,
    item_name: str | None = None,
    force: bool = False,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "category_territory_rule_set"):
        return
    guild = interaction.guild
    assert guild is not None

    if scope == "global":
        if rule == "territory_link":
            bot.database.set_global_territory_requirement(guild.id, required, force=force)
        else:
            bot.database.set_global_stat_requirement(guild.id, required, force=force)
        await record_command_event(
            interaction,
            "category_territory_rule_set",
            details={"rule": rule, "scope": scope, "required": required, "force": force},
        )
        title = "Global Territory Rule Updated" if rule == "territory_link" else "Global Stat Rule Updated"
        behavior_lines = (
            [
                "When required is true, items must be linked to a territory or clan to be rolled.",
                "When false, items are available from anywhere unless a lower scope overrides them.",
            ]
            if rule == "territory_link"
            else [
                "When required is true, /roll_forage must include `stat` for this scope.",
                "If false, stat input is optional unless a lower scope or item stat threshold requires it.",
            ]
        )
        await interaction.response.send_message(
            embed=clean_embed(
                title,
                [
                    f"Global required: `{required}`.",
                    f"Force propagate: `{force}`.",
                    *behavior_lines,
                ],
            )
        )
        return

    if category is None:
        await interaction.response.send_message(
            embed=clean_embed("Category Required", ["Provide `category` for category/item scope."]),
            ephemeral=True,
        )
        return

    if scope == "category":
        try:
            if rule == "territory_link":
                normalized_category = bot.database.set_category_territory_requirement(
                    guild.id,
                    category,
                    required,
                    force=force,
                )
            else:
                normalized_category = bot.database.set_category_stat_requirement(
                    guild.id,
                    category,
                    required,
                    force=force,
                )
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Update Failed", [str(exc)]), ephemeral=True)
            return
        await record_command_event(
            interaction,
            "category_territory_rule_set",
            details={"rule": rule, "scope": scope, "category": normalized_category, "required": required, "force": force},
        )
        title = "Category Territory Rule Updated" if rule == "territory_link" else "Category Stat Rule Updated"
        behavior_line = (
            "Required means items in this category need a territory link to be rolled."
            if rule == "territory_link"
            else "Required means /roll_forage must include stat for this category."
        )
        await interaction.response.send_message(
            embed=clean_embed(
                title,
                [
                    f"Category: `{normalized_category}`.",
                    f"Required: `{required}`.",
                    f"Force propagate to item rules: `{force}`.",
                    behavior_line,
                ],
            )
        )
        return

    if item_name is None:
        await interaction.response.send_message(
            embed=clean_embed("Item Required", ["Provide `item_name` when scope is `item`." ]),
            ephemeral=True,
        )
        return

    try:
        if rule == "territory_link":
            item = bot.database.set_item_territory_requirement(guild.id, category, item_name, required)
        else:
            item = bot.database.set_item_stat_requirement(guild.id, category, item_name, required)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Update Failed", [str(exc)]), ephemeral=True)
        return
    await record_command_event(
        interaction,
        "category_territory_rule_set",
        details={"rule": rule, "scope": scope, "category": item.category, "item_name": item.name, "required": required},
    )
    title = "Item Territory Rule Updated" if rule == "territory_link" else "Item Stat Rule Updated"
    behavior_line = (
        "Required means this item must be linked to a territory to be rolled."
        if rule == "territory_link"
        else "Required means /roll_forage must include stat for this item."
    )
    await interaction.response.send_message(
        embed=clean_embed(
            title,
            [
                f"Category: `{item.category}`.",
                f"Item: `{item.name}`.",
                f"Required: `{required}`.",
                behavior_line,
            ],
        )
    )


@app_commands.command(name="category_remove_request", description="Request deletion of a category (requires confirm).")
@app_commands.describe(
    category="Category to delete",
)
async def category_remove_request(interaction: discord.Interaction, category: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "category_remove_request"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "category_remove_request", details={"category": category.lower()})
    try:
        expires = bot.database.request_category_removal(guild.id, interaction.user.id, category)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Cannot Request Removal", [str(exc)]), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=clean_embed(
            "Removal Requested",
            [
                f"Category `{category.lower()}` is pending deletion.",
                "This deletes all items in that category and related storage/link rows.",
                f"Confirm with `/category_remove_confirm category: {category.lower()}` before `{expires}` UTC.",
            ],
        ),
        ephemeral=True,
    )


@app_commands.command(name="category_remove_confirm", description="Confirm deletion of a requested category.")
@app_commands.describe(
    category="Category to confirm deletion for",
)
async def category_remove_confirm(interaction: discord.Interaction, category: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "category_remove_confirm"):
        return
    guild = interaction.guild
    assert guild is not None
    try:
        deleted = bot.database.confirm_category_removal(guild.id, interaction.user.id, category)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Removal Failed", [str(exc)]), ephemeral=True)
        return
    await record_command_event(interaction, "category_remove_confirm", details={"category": category.lower(), **deleted})
    await interaction.response.send_message(
        embed=clean_embed(
            "Category Removed",
            [
                f"Category: `{category.lower()}`.",
                f"Items deleted: `{deleted['items_deleted']}`.",
                f"Territory links deleted: `{deleted['links_deleted']}`.",
                f"Storage rows deleted: `{deleted['storage_rows_deleted']}`.",
            ],
        )
    )


@app_commands.command(name="item_add", description="Add or update an item in a category.")
@app_commands.describe(
    category="Category of this item",
    name="Item name (e.g. tansy, mouse)",
    enabled="Whether this item can appear in forage rolls",
    required_stat="Minimum stat required to catch/find this item (optional)",
    required_stat_name="Name of the stat used for required_stat checks (optional)",
)
async def item_add(
    interaction: discord.Interaction,
    category: CategoryLiteral,
    name: str,
    enabled: bool = True,
    required_stat: app_commands.Range[int, 0, 1000] | None = None,
    required_stat_name: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "item_add"):
        return
    guild = interaction.guild
    assert guild is not None
    try:
        normalized_cat = bot.database.require_category(guild.id, category)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Unknown Category", [str(exc)]), ephemeral=True)
        return
    if bot.database.get_item(guild.id, normalized_cat, name.lower().strip()) is not None:
        await interaction.response.send_message(
            embed=clean_embed("Item Already Exists", [
                f"`{name.lower().strip()}` already exists in category `{normalized_cat}`.",
                "Use `/item_edit` to modify it.",
            ]),
            ephemeral=True,
        )
        return
    await record_command_event(interaction, "item_add")
    item = bot.database.add_item(
        guild.id,
        name,
        category,
        enabled=enabled,
        required_stat=required_stat,
        required_stat_name=required_stat_name,
    )
    await interaction.response.send_message(
        embed=clean_embed(
            "Item Saved",
            [
                f"Item: `{item.name}`.",
                f"Category: `{item.category}`.",
                f"Enabled: `{item.enabled}`.",
                f"Required stat: `{item.required_stat_name or 'any'} {item.required_stat if item.required_stat is not None else 'none'}`.",
            ],
        )
    )


@app_commands.command(name="item_edit", description="Edit an existing item's name, stat requirement, or enabled state.")
@app_commands.describe(
    category="Category of the item to edit",
    name="Current item name",
    new_name="Rename the item to this (optional)",
    enabled="Enable or disable this item for forage rolls (optional)",
    required_stat="Set minimum stat required to catch/find this item (optional)",
    required_stat_name="Set required stat name for this item (optional)",
)
async def item_edit(
    interaction: discord.Interaction,
    category: CategoryLiteral,
    name: str,
    new_name: str | None = None,
    enabled: bool | None = None,
    required_stat: app_commands.Range[int, 0, 1000] | None = None,
    required_stat_name: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "item_edit"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "item_edit")
    if new_name is None and enabled is None and required_stat is None and required_stat_name is None:
        await interaction.response.send_message(
            embed=clean_embed("No Changes", ["Provide at least one of: `new_name`, `enabled`, `required_stat`, or `required_stat_name`."]),
            ephemeral=True,
        )
        return
    try:
        item = bot.database.update_item(
            guild.id,
            category,
            name,
            new_name=new_name,
            enabled=enabled,
            required_stat=required_stat,
            required_stat_name=required_stat_name,
        )
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Item Not Found", [str(exc)]), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=clean_embed(
            "Item Updated",
            [
                f"Item: `{item.name}`.",
                f"Category: `{item.category}`.",
                f"Enabled: `{item.enabled}`.",
                f"Required stat: `{item.required_stat_name or 'any'} {item.required_stat if item.required_stat is not None else 'none'}`.",
            ],
        )
    )


@app_commands.command(name="item_remove", description="Remove an item from a category.")
@app_commands.describe(
    category="Category of this item",
    name="Name of the item to remove",
)
async def item_remove(interaction: discord.Interaction, category: CategoryLiteral, name: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "item_remove"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "item_remove")
    removed = bot.database.remove_item(guild.id, category, name)
    if removed:
        await interaction.response.send_message(embed=clean_embed("Item Removed", [f"Removed `{name.lower()}` from `{category}`."]))
    else:
        await interaction.response.send_message(embed=clean_embed("Item Not Found", [f"No `{category}` item named `{name.lower()}` exists."]))


@app_commands.command(name="item_list", description="List configured items, optionally filtered by category.")
@app_commands.describe(
    category="Category to filter by; leave blank for all",
)
async def item_list(interaction: discord.Interaction, category: CategoryLiteral | None = None) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "item_list")
    items = bot.database.list_items(guild.id, category)
    if not items:
        await interaction.response.send_message(embed=clean_embed("No Items", ["No items are configured yet."]))
        return
    lines = [
        f"`{item.category}` `{item.name}` - enabled `{item.enabled}` - stat `{item.required_stat_name or 'any'} {item.required_stat if item.required_stat is not None else 'none'}`"
        for item in items
    ]
    await interaction.response.send_message(embed=clean_embed("Items", lines))


@app_commands.command(name="clan_item_link", description="Link or unlink an item across all territories owned by a clan.")
@app_commands.describe(
    clan="Clan whose territory set receives this item link",
    category="Category of the item",
    item_name="Item name to link",
    weight="Drop weight for this clan link",
    seasonal_modifier="Seasonal multiplier for this clan link",
    remove="Set true to remove this clan link",
)
async def clan_item_link(
    interaction: discord.Interaction,
    clan: str,
    category: CategoryLiteral,
    item_name: str,
    weight: app_commands.Range[float, 0.1, 100.0] = 1.0,
    seasonal_modifier: app_commands.Range[float, 0.1, 10.0] = 1.0,
    remove: bool = False,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "clan_item_link"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(
        interaction,
        "clan_item_link",
        clan_name=clan,
        details={"category": category, "item_name": item_name.lower(), "remove": remove, "weight": weight},
    )
    try:
        if remove:
            links = bot.database.list_clan_item_links(guild.id, clan)
            target = [
                link
                for link in links
                if str(link["category"]) == category and str(link["item_name"]) == item_name.lower()
            ]
            if not target:
                await interaction.response.send_message(
                    embed=clean_embed("No Link Found", [f"No clan link exists for `{category}` `{item_name.lower()}` on `{clan.lower()}`."]),
                    ephemeral=True,
                )
                return
            clan_obj = bot.database.require_clan(guild.id, clan)
            item = bot.database.require_item(guild.id, category, item_name)
            with bot.database.connect() as connection:
                connection.execute(
                    "DELETE FROM clan_item_links WHERE clan_id = ? AND item_id = ?",
                    (clan_obj.id, item.id),
                )
            await interaction.response.send_message(
                embed=clean_embed("Clan Link Removed", [f"Removed `{category}` `{item.name}` from `{clan_obj.name}`."])
            )
            return

        bot.database.set_clan_item_link(
            guild.id,
            clan,
            category,
            item_name,
            weight,
            seasonal_modifier=seasonal_modifier,
        )
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Link Failed", [str(exc)]), ephemeral=True)
        return
    await interaction.response.send_message(
        embed=clean_embed(
            "Clan Link Saved",
            [
                f"Clan: `{clan.lower()}`.",
                f"Item: `{item_name.lower()}` ({category}).",
                f"Weight: `{weight}`.",
                f"Seasonal modifier: `{seasonal_modifier}`.",
            ],
        )
    )


@app_commands.command(name="territory_create", description="Create a territory and optionally bind it to a clan.")
@app_commands.describe(
    name="Name of the territory (e.g. pineforest)",
    clan="Clan that owns this territory; optional",
    linked_channel="Optional channel/thread ID or Discord channel link to auto-detect this territory",
)
async def territory_create(
    interaction: discord.Interaction,
    name: str,
    clan: str | None = None,
    linked_channel: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_create"):
        return
    guild = interaction.guild
    assert guild is not None

    linked_channel_id: int | None = None
    if linked_channel is not None:
        linked_channel_id = parse_channel_id_from_input(linked_channel)
        if linked_channel_id is None:
            await interaction.response.send_message(
                embed=clean_embed(
                    "Invalid Channel Link",
                    [
                        "Provide a channel/thread ID or paste a channel link.",
                        "Example: `https://discord.com/channels/<guild>/<channel>/<thread?>`.",
                    ],
                ),
                ephemeral=True,
            )
            return

    await record_command_event(interaction, "territory_create", clan_name=clan or name)
    territory_id = bot.database.create_territory(guild.id, name, clan, channel_id=linked_channel_id)
    owner_line = f"Owner clan: `{clan.lower()}`." if clan else "Owner clan: `none`."
    link_line = (
        f"Linked channel: `<#{linked_channel_id}>` ({linked_channel_id})."
        if linked_channel_id is not None
        else "Linked channel: `none`."
    )
    await interaction.response.send_message(
        embed=clean_embed(
            "Territory Created",
            [
                f"Name: `{name.lower()}`.",
                f"ID: `{territory_id}`.",
                owner_line,
                link_line,
            ],
        )
    )


@app_commands.command(name="territory_item_set", description="Set an item's drop weight for a territory.")
@app_commands.describe(
    territory="Territory to configure",
    category="Category of this item",
    item_name="Item whose drop rate you are adjusting",
    weight="Relative likelihood this item is found in this territory. Higher = more common. "
           "E.g. weight 4 is four times as likely to appear as weight 1. Range: 0.1–100.",
    seasonal_modifier="Seasonal multiplier applied to weight (default 1.0).",
)
async def territory_item_set(
    interaction: discord.Interaction,
    territory: str,
    category: CategoryLiteral,
    item_name: str,
    weight: app_commands.Range[float, 0.1, 100.0],
    seasonal_modifier: app_commands.Range[float, 0.1, 10.0] = 1.0,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_item_set"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "territory_item_set", details={"territory": territory.lower(), "item_name": item_name.lower()})
    bot.database.set_territory_item_weight(
        guild.id,
        territory,
        category,
        item_name,
        weight,
        seasonal_modifier=seasonal_modifier,
    )
    await interaction.response.send_message(
        embed=clean_embed(
            "Territory Weight Updated",
            [
                f"Territory: `{territory.lower()}`.",
                f"Category: `{category}`.",
                f"Item: `{item_name.lower()}`.",
                f"Weight: `{weight}`.",
                f"Seasonal modifier: `{seasonal_modifier}`.",
                f"Effective weight: `{weight * seasonal_modifier}`.",
            ],
        )
    )


@app_commands.command(name="territory_item_set_bulk", description="Set one item weight across multiple territories.")
@app_commands.describe(
    territories="Comma-separated territory names (e.g. forest, lake, canyon)",
    category="Category of this item",
    item_name="Item whose drop rate you are adjusting",
    weight="Relative likelihood for each listed territory (0.1 to 100)",
    seasonal_modifier="Seasonal multiplier applied to weight for all listed territories (default 1.0)",
)
async def territory_item_set_bulk(
    interaction: discord.Interaction,
    territories: str,
    category: CategoryLiteral,
    item_name: str,
    weight: app_commands.Range[float, 0.1, 100.0],
    seasonal_modifier: app_commands.Range[float, 0.1, 10.0] = 1.0,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_item_set_bulk"):
        return
    guild = interaction.guild
    assert guild is not None

    territory_names = parse_territory_names(territories)
    if not territory_names:
        await interaction.response.send_message(
            embed=clean_embed("No Territories Provided", ["Add one or more territory names, separated by commas."]),
            ephemeral=True,
        )
        return

    missing = [name for name in territory_names if bot.database.get_territory(guild.id, name) is None]
    if missing:
        await interaction.response.send_message(
            embed=clean_embed(
                "Unknown Territories",
                [
                    f"Missing: `{', '.join(missing)}`.",
                    "Create them first with `/territory_create`.",
                ],
            ),
            ephemeral=True,
        )
        return

    for territory_name in territory_names:
        bot.database.set_territory_item_weight(
            guild.id,
            territory_name,
            category,
            item_name,
            weight,
            seasonal_modifier=seasonal_modifier,
        )

    await record_command_event(
        interaction,
        "territory_item_set_bulk",
        details={
            "territories": territory_names,
            "item_name": item_name.lower(),
            "weight": weight,
            "seasonal_modifier": seasonal_modifier,
            "category": category,
        },
    )
    await interaction.response.send_message(
        embed=clean_embed(
            "Bulk Territory Weights Updated",
            [
                f"Item: `{item_name.lower()}` ({category}).",
                f"Weight: `{weight}`.",
                f"Seasonal modifier: `{seasonal_modifier}`.",
                f"Effective weight: `{weight * seasonal_modifier}`.",
                f"Territories: `{', '.join(territory_names)}`.",
            ],
        )
    )


@app_commands.command(name="territory_item_seasonal_set", description="Update the seasonal modifier for an item already linked to a territory.")
@app_commands.describe(
    territory="Territory whose link you want to adjust",
    category="Category of this item",
    item_name="Item whose seasonal modifier you are changing",
    seasonal_modifier="New seasonal multiplier (0.1–10); effective weight = weight × seasonal modifier",
)
async def territory_item_seasonal_set(
    interaction: discord.Interaction,
    territory: str,
    category: CategoryLiteral,
    item_name: str,
    seasonal_modifier: app_commands.Range[float, 0.1, 10.0],
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_item_seasonal_set"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(
        interaction, "territory_item_seasonal_set",
        details={"territory": territory.lower(), "item_name": item_name.lower(), "seasonal_modifier": seasonal_modifier},
    )
    links = bot.database.list_territory_items(guild.id, territory, category)
    existing = next((e for e in links if e.item_name == item_name.lower()), None)
    if existing is None:
        await interaction.response.send_message(
            embed=clean_embed(
                "Link Not Found",
                [
                    f"No `{category}` link for `{item_name.lower()}` in territory `{territory.lower()}`.",
                    "Use `/territory_item_set` to create the link first.",
                ],
            ),
            ephemeral=True,
        )
        return
    bot.database.set_territory_item_weight(
        guild.id, territory, category, item_name, existing.weight, seasonal_modifier=seasonal_modifier
    )
    await interaction.response.send_message(
        embed=clean_embed(
            "Seasonal Modifier Updated",
            [
                f"Territory: `{territory.lower()}`.",
                f"Category: `{category}`.",
                f"Item: `{item_name.lower()}`.",
                f"Weight: `{existing.weight}` (unchanged).",
                f"Seasonal modifier: `{seasonal_modifier}`.",
                f"Effective weight: `{existing.weight * seasonal_modifier}`.",
            ],
        )
    )


@app_commands.command(name="territory_item_remove", description="Remove an item's drop link from a territory.")
@app_commands.describe(
    territory="Territory to remove the link from",
    category="Category of this item",
    item_name="Item to unlink",
)
async def territory_item_remove(
    interaction: discord.Interaction,
    territory: str,
    category: CategoryLiteral,
    item_name: str,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_item_remove"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(
        interaction, "territory_item_remove",
        details={"territory": territory.lower(), "item_name": item_name.lower()},
    )
    try:
        removed = bot.database.remove_territory_item(guild.id, territory, category, item_name)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Error", [str(exc)]), ephemeral=True)
        return
    if removed:
        await interaction.response.send_message(
            embed=clean_embed(
                "Item Link Removed",
                [
                    f"Removed `{category}` `{item_name.lower()}` from territory `{territory.lower()}`.",
                    "The item itself is not deleted; only the territory–item link was removed.",
                ],
            )
        )
    else:
        await interaction.response.send_message(
            embed=clean_embed(
                "Link Not Found",
                [f"No `{category}` link for `{item_name.lower()}` in territory `{territory.lower()}`."]
            ),
            ephemeral=True,
        )


@app_commands.command(name="seasonal_modifier_set", description="Set the seasonal multiplier for a clan or server-wide.")
@app_commands.describe(
    scope="'clan' to set it for one clan only; 'global' to apply server-wide",
    value="New seasonal multiplier (0.01–10.0); stacks with territory-item and other seasonal modifiers",
    clan="Clan to set the modifier for (required when scope is clan)",
)
async def seasonal_modifier_set(
    interaction: discord.Interaction,
    scope: Literal["clan", "global"],
    value: app_commands.Range[float, 0.01, 10.0],
    clan: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "seasonal_modifier_set"):
        return
    guild = interaction.guild
    assert guild is not None
    if scope == "clan":
        if clan is None:
            await interaction.response.send_message(
                embed=clean_embed("Clan Required", ["Provide a `clan` name when scope is `clan`."]),
                ephemeral=True,
            )
            return
        try:
            updated = bot.database.update_clan_config(guild.id, clan, seasonal_modifier=value)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Error", [str(exc)]), ephemeral=True)
            return
        await record_command_event(interaction, "seasonal_modifier_set", clan_name=clan, details={"scope": "clan", "value": value})
        await interaction.response.send_message(
            embed=clean_embed(
                "Clan Seasonal Modifier Updated",
                [
                    f"Clan: `{updated.name}`.",
                    f"Clan seasonal modifier: `{updated.seasonal_modifier}`.",
                    "Stacks with per-territory-item seasonal modifiers and the global modifier.",
                ],
            )
        )
    else:
        bot.database.set_global_seasonal_modifier(guild.id, value)
        await record_command_event(interaction, "seasonal_modifier_set", details={"scope": "global", "value": value})
        await interaction.response.send_message(
            embed=clean_embed(
                "Global Seasonal Modifier Updated",
                [
                    f"Global seasonal modifier: `{value}`.",
                    "Applies to every roll in this server on top of per-territory-item and per-clan modifiers.",
                ],
            )
        )


@app_commands.command(name="weight_breakdown", description="Show effective item weights for a clan's territories with formula breakdown.")
@app_commands.describe(
    clan="Clan to inspect (required); shows only territories assigned to this clan",
    category="Filter to a specific category; leave blank for all",
    territory="Filter to a specific territory; leave blank for all clan territories",
    item_name="Filter to a specific item; leave blank for all",
)
async def weight_breakdown(
    interaction: discord.Interaction,
    clan: str,
    category: CategoryLiteral | None = None,
    territory: str | None = None,
    item_name: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "weight_breakdown"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "weight_breakdown", clan_name=clan)
    try:
        rows = bot.database.get_weight_breakdown(
            guild.id, clan, category=category, territory_name=territory, item_name=item_name
        )
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Error", [str(exc)]), ephemeral=True)
        return
    if not rows:
        await interaction.response.send_message(
            embed=clean_embed("No Data", ["No territory–item links found for the given filters. Ensure territories are assigned to this clan."]),
            ephemeral=True,
        )
        return
    # Compute normalized 0.0–1.0 probability per territory+category group.
    group_sums: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (str(row["territory_name"]), str(row["category"]))
        group_sums[key] = group_sums.get(key, 0.0) + float(row["effective_weight"])
    clan_obj = bot.database.get_clan(guild.id, clan.lower())
    cs_display = clan_obj.seasonal_modifier if clan_obj else 1.0
    gs_display = bot.database.get_global_seasonal_modifier(guild.id)
    lines: list[str] = [
        f"Clan: `{clan.lower()}` · clan seasonal: `{cs_display}` · global seasonal: `{gs_display}`",
        "Formula: `t_weight × t_seasonal × clan_seasonal × global_seasonal = eff → probability`",
    ]
    current_territory: str | None = None
    for row in rows:
        territory_name_r = str(row["territory_name"])
        if territory_name_r != current_territory:
            current_territory = territory_name_r
            lines.append(f"**{territory_name_r}**")
        key = (territory_name_r, str(row["category"]))
        total_eff = group_sums[key]
        prob = float(row["effective_weight"]) / total_eff if total_eff > 0 else 0.0
        lines.append(
            f"  `{row['item_name']}` ({row['category']}): "
            f"`{row['weight']}` × `{row['territory_seasonal_modifier']}` × `{row['clan_seasonal_modifier']}` × `{row['global_seasonal_modifier']}`"
            f" = `{float(row['effective_weight']):.3f}` → `{prob:.3f}`"
        )
    embeds = chunk_lines_for_embed(f"Weight Breakdown: {clan.lower()}", lines)
    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@app_commands.command(name="territory_link_set", description="Link or clear a territory's auto-detect channel.")
@app_commands.describe(
    territory="Territory to link",
    channel_or_link="Channel/thread ID, channel link, or 'clear' to remove the link",
)
async def territory_link_set(interaction: discord.Interaction, territory: str, channel_or_link: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_link_set"):
        return
    guild = interaction.guild
    assert guild is not None

    action = channel_or_link.strip().lower()
    if action in {"clear", "none", "remove"}:
        bot.database.set_territory_channel_link(guild.id, territory, None)
        await record_command_event(interaction, "territory_link_set", details={"territory": territory.lower(), "channel_id": None})
        await interaction.response.send_message(
            embed=clean_embed("Territory Link Cleared", [f"`{territory.lower()}` no longer has a linked channel."])
        )
        return

    channel_id = parse_channel_id_from_input(channel_or_link)
    if channel_id is None:
        await interaction.response.send_message(
            embed=clean_embed(
                "Invalid Channel Link",
                [
                    "Provide a channel/thread ID or paste a channel link.",
                    "Use `clear` to remove an existing link.",
                ],
            ),
            ephemeral=True,
        )
        return

    bot.database.set_territory_channel_link(guild.id, territory, channel_id)
    await record_command_event(interaction, "territory_link_set", details={"territory": territory.lower(), "channel_id": channel_id})
    await interaction.response.send_message(
        embed=clean_embed(
            "Territory Linked",
            [
                f"Territory: `{territory.lower()}`.",
                f"Linked channel: `<#{channel_id}>` ({channel_id}).",
            ],
        )
    )


@app_commands.command(name="territory_link_show", description="Show territory-to-channel auto-detect links.")
@app_commands.describe(
    territory="Optional territory to inspect; leave blank to list all",
)
async def territory_link_show(interaction: discord.Interaction, territory: str | None = None) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_link_show"):
        return
    guild = interaction.guild
    assert guild is not None

    if territory is not None:
        row = bot.database.get_territory(guild.id, territory)
        if row is None:
            await interaction.response.send_message(
                embed=clean_embed("Unknown Territory", [f"`{territory.lower()}` does not exist."]),
                ephemeral=True,
            )
            return
        channel_id = int(row["channel_id"]) if row["channel_id"] is not None else None
        lines = [f"Territory: `{str(row['name'])}`."]
        if channel_id is None:
            lines.append("Linked channel: `none`.")
        else:
            lines.append(f"Linked channel: `<#{channel_id}>` ({channel_id}).")
        await record_command_event(interaction, "territory_link_show", details={"territory": territory.lower()})
        await interaction.response.send_message(embed=clean_embed("Territory Link", lines))
        return

    links = bot.database.list_territory_channel_links(guild.id)
    if not links:
        await interaction.response.send_message(
            embed=clean_embed("Territory Links", ["No territories exist yet."]),
            ephemeral=True,
        )
        return
    lines: list[str] = []
    for territory_name, channel_id in links:
        if channel_id is None:
            lines.append(f"`{territory_name}` -> `none`")
        else:
            lines.append(f"`{territory_name}` -> <#{channel_id}> (`{channel_id}`)")
    await record_command_event(interaction, "territory_link_show")
    embeds = chunk_lines_for_embed("Territory Links", lines)
    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@app_commands.command(name="territory_show", description="Show weighted items for a territory.")
@app_commands.describe(
    territory="Territory to inspect",
    category="Category to show weighted items for",
)
async def territory_show(interaction: discord.Interaction, territory: str, category: CategoryLiteral) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "territory_show", details={"territory": territory.lower()})
    items = bot.database.list_territory_items(guild.id, territory, category)
    territory_row = bot.database.get_territory(guild.id, territory)
    linked_channel_id = int(territory_row["channel_id"]) if territory_row and territory_row["channel_id"] is not None else None
    if not items:
        extra = f"Linked channel: `<#{linked_channel_id}>` ({linked_channel_id})." if linked_channel_id is not None else "Linked channel: `none`."
        await interaction.response.send_message(
            embed=clean_embed("No Territory Weights", ["No weighted items are configured for that territory/category yet.", extra])
        )
        return
    lines = [
        f"`{entry.item_name}` - weight `{entry.weight}` x seasonal `{entry.seasonal_modifier}` = `{entry.effective_weight}`"
        for entry in items
    ]
    if linked_channel_id is not None:
        lines.append(f"Linked channel: `<#{linked_channel_id}>` ({linked_channel_id})")
    else:
        lines.append("Linked channel: `none`")
    await interaction.response.send_message(embed=clean_embed(f"Territory Weights: {territory.lower()}", lines))


@app_commands.command(name="linkage_show", description="Show item–territory linkages grouped by category, territory, or clan/region.")
@app_commands.describe(
    view="Use `territory`, `clan`, or a category name",
    name="Optional item/territory name filter",
)
async def linkage_show(
    interaction: discord.Interaction,
    view: str,
    name: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "linkage_show"):
        return
    guild = interaction.guild
    assert guild is not None

    normalized_view = view.lower().strip()
    filter_name = name.lower() if name else None
    lines: list[str] = []
    if normalized_view == "clan":
        rows = bot.database.list_all_territory_item_links(guild.id)
        if filter_name:
            rows = [row for row in rows if str(row.get("clan_name", "")).lower() == filter_name]
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            clan_label = str(row.get("clan_name") or "unassigned")
            grouped.setdefault(clan_label, []).append(row)
        if not grouped:
            await interaction.response.send_message(
                embed=clean_embed("No Linkages", ["No clan/region linkage rows matched this filter."]),
                ephemeral=True,
            )
            return
        for clan_label in sorted(grouped.keys()):
            lines.append(f"`{clan_label}`")
            for row in grouped[clan_label]:
                lines.append(
                    f"  `{row['territory_name']}` -> `{row['category']}` `{row['item_name']}`: `{row['weight']}` x `{row['seasonal_modifier']}` = `{row['effective_weight']}`"
                )
    elif normalized_view != "territory":
        try:
            normalized_view = bot.database.require_category(guild.id, normalized_view)
        except ValueError as exc:
            await interaction.response.send_message(
                embed=clean_embed("Invalid View", [str(exc), "Use a category name or `territory`."]),
                ephemeral=True,
            )
            return
        rows = bot.database.list_item_links(guild.id, normalized_view, item_name=filter_name)
        if not rows:
            await interaction.response.send_message(
                embed=clean_embed("No Linkages", [f"No `{normalized_view}` linkage rows matched this filter."]),
                ephemeral=True,
            )
            return
        current_item = ""
        for row in rows:
            item_name = str(row["item_name"])
            if item_name != current_item:
                current_item = item_name
                lines.append(f"`{item_name}`")
            lines.append(
                f"  -> `{row['territory_name']}`: `{row['weight']}` x `{row['seasonal_modifier']}` = `{row['effective_weight']}`"
            )
    else:
        rows = bot.database.list_all_territory_item_links(guild.id)
        if filter_name:
            rows = [row for row in rows if str(row["territory_name"]) == filter_name]
        if not rows:
            await interaction.response.send_message(
                embed=clean_embed("No Linkages", ["No territory linkage rows matched this filter."]),
                ephemeral=True,
            )
            return
        current_territory = ""
        for row in rows:
            territory_name = str(row["territory_name"])
            if territory_name != current_territory:
                current_territory = territory_name
                lines.append(f"`{territory_name}`")
            lines.append(
                f"  `{row['category']}` `{row['item_name']}`: `{row['weight']}` x `{row['seasonal_modifier']}` = `{row['effective_weight']}`"
            )

    await record_command_event(interaction, "linkage_show", details={"view": normalized_view, "name": filter_name})
    embeds = chunk_lines_for_embed("Linkage View", lines)
    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@app_commands.command(name="territory_link_validate", description="Validate territory and category linkage coverage.")
@app_commands.describe(
    scope="Choose which linkage scope to validate",
    category="When scope is `category`, validate only that category",
)
async def territory_link_validate(
    interaction: discord.Interaction,
    scope: Literal["all", "territory", "category"] = "all",
    category: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "territory_link_validate"):
        return
    guild = interaction.guild
    assert guild is not None

    all_issues = build_linkage_validation_report(bot, guild.id)
    if scope == "territory":
        issues = [issue for issue in all_issues if issue.startswith("Territory")]
    elif scope == "category":
        if not category:
            await interaction.response.send_message(
                embed=clean_embed("Missing Category", ["Provide a `category` when scope is `category`." ]),
                ephemeral=True,
            )
            return
        try:
            normalized_category = bot.database.require_category(guild.id, category)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Invalid Category", [str(exc)]), ephemeral=True)
            return
        issues = [issue for issue in all_issues if f"`{normalized_category}`" in issue]
    else:
        issues = all_issues

    await record_command_event(
        interaction,
        "territory_link_validate",
        details={"scope": scope, "category": category.lower() if category else None, "issues": len(issues)},
    )
    if not issues:
        await interaction.response.send_message(embed=clean_embed("Linkage Validation", ["No issues found."]))
        return
    embeds = chunk_lines_for_embed("Linkage Validation Issues", issues)
    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@app_commands.command(name="system_check", description="Run a broad configuration and linkage health check.")
async def system_check(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "system_check"):
        return
    guild = interaction.guild
    assert guild is not None

    issues = build_linkage_validation_report(bot, guild.id)
    clans = bot.database.list_clans(guild.id)
    for clan in clans:
        if clan.tracking_mode in {TrackingMode.FORUM.value, TrackingMode.SPREADSHEET.value} and not clan.tracking_link:
            issues.append(f"Clan `{clan.name}` uses `{clan.tracking_mode}` but has no tracking link.")
    if not clans:
        issues.append("No clans are configured.")

    await record_command_event(interaction, "system_check", details={"issues": len(issues)})
    if not issues:
        await interaction.response.send_message(embed=clean_embed("System Check", ["No issues found."]))
        return
    embeds = chunk_lines_for_embed("System Check Issues", issues)
    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@app_commands.command(name="import_csv_examples", description="Show example CSV formats for imports.")
async def import_csv_examples(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "import_csv_examples"):
        return
    await record_command_event(interaction, "import_csv_examples")
    lines = [
        "Territories CSV:",
        "`name,clan,channel_id`",
        "`pineforest,birchclan,123456789012345678`",
        "`riverbend,,`",
        "",
        "Items CSV:",
        "`category,name,enabled,required_stat_name,required_stat`",
        "`prey,mouse,true,,`",
        "`herb,rabbit,true,strength,8`",
        "",
        "Links CSV:",
        "`territory,category,item_name,weight,seasonal_modifier`",
        "`pineforest,herb,tansy,4,1.25`",
        "`riverbend,prey,mouse,3,0.8`",
    ]
    await interaction.response.send_message(embed=clean_embed("CSV Examples", lines), ephemeral=True)


@app_commands.command(name="import", description="Unified import command for CSV, forum, or thread sources.")
@app_commands.describe(
    type="Import source type",
    link="CSV URL/text, forum message link, or thread link/ID",
    category="Category for imported items (required for forum/thread)",
    stats="If false, ignore imported stat requirements",
    clan_region="Fallback clan/region when source does not specify one",
    confirm="True applies changes, False previews only",
)
async def import_items(
    interaction: discord.Interaction,
    type: Literal["csv", "forum", "thread"],
    link: str,
    category: str | None = None,
    stats: bool = True,
    clan_region: str | None = None,
    confirm: bool = False,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "import"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    if type == "csv":
        source_data = ""
        rows: list[dict[str, str]]
        if re.match(r"https?://", link.strip(), flags=re.IGNORECASE):
            try:
                raw = fetch_tracking_bytes(link)
                rows = parse_spreadsheet_rows_from_bytes(raw)
            except Exception as error:
                await interaction.response.send_message(embed=clean_embed("Import Failed", [str(error)]), ephemeral=True)
                return
        else:
            source_data = link
            try:
                rows = parse_csv_rows(source_data)
            except ValueError as error:
                await interaction.response.send_message(embed=clean_embed("Import Failed", [str(error)]), ephemeral=True)
                return

        target = "items"
        processed = 0
        actions: list[str] = []
        errors: list[str] = []
        for index, row in enumerate(rows, start=2):
            try:
                if not stats:
                    row = {**row, "required_stat": "", "required_stat_name": ""}
                action = process_catalog_import_row(bot, guild.id, target, row, apply_changes=confirm)
                processed += 1
                actions.append(action)
            except Exception as error:
                errors.append(f"Row `{index}`: {error}")

        await record_command_event(
            interaction,
            "import",
            details={"type": type, "confirm": confirm, "processed": processed, "errors": len(errors), "stats": stats},
        )
        embed_title = "Import Result" if confirm else "Import Preview"
        lines = [f"Type: `{type}`.", f"Rows parsed: `{len(rows)}`.", f"Valid rows: `{processed}`."]
        if not confirm:
            lines.append(":warning: Nothing was imported. Re-run with `confirm: True` to apply.")
        if actions:
            lines.append("Applied actions:" if confirm else "Planned actions:")
            lines.extend(actions[:15])
        if errors:
            lines.append(f"Errors: `{len(errors)}`.")
            lines.extend(errors[:10])
        await interaction.response.send_message(embed=clean_embed(embed_title, lines), ephemeral=True)
        return

    if category is None:
        await interaction.response.send_message(
            embed=clean_embed("Missing Category", ["`category` is required for forum/thread imports."]),
            ephemeral=True,
        )
        return

    if type == "forum":
        await _import_forum_catalog_internal(
            interaction,
            forum_post_link=link,
            category=category,
            confirm=confirm,
            try_link_territories=False,
            weight=1.0,
            clan_fallback=clan_region,
            include_stats=stats,
        )
        return

    await _import_thread_catalog_internal(
        interaction,
        thread=link,
        category=category,
        confirm=confirm,
        import_items=None,
        weight=1.0,
        clan_fallback=clan_region,
        include_stats=stats,
    )


@app_commands.command(name="preview_message_link", description="Fetch a linked Discord message and report what the bot can read from it.")
@app_commands.describe(link="Discord message link to inspect")
async def preview_message_link(interaction: discord.Interaction, link: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "preview_message_link"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    try:
        target_channel, message = await resolve_message_from_link(guild, link)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Inspect Failed", [str(exc)]), ephemeral=True)
        return

    await record_command_event(
        interaction,
        "preview_message_link",
        details={"link": link, "channel_id": target_channel.id, "message_id": message.id},
    )

    lines = [
        f"Message ID: `{message.id}`.",
        f"Channel: `{getattr(target_channel, 'name', 'unknown')}` (`{target_channel.id}` / `{type(target_channel).__name__}`).",
        f"Author: `{message.author}` (`{message.author.id}`).",
        f"Content chars: `{len(message.content or '')}`.",
        f"Content preview: `{summarize_message_text(message.content)}`.",
        f"Embeds: `{len(message.embeds)}`.",
        f"Attachments: `{len(message.attachments)}`.",
    ]
    if isinstance(target_channel, discord.Thread) and target_channel.parent is not None:
        lines.append(f"Parent channel: `{target_channel.parent.name}` (`{target_channel.parent.id}`).")
    if message.embeds:
        first_embed = message.embeds[0]
        lines.append(f"First embed title: `{summarize_message_text(first_embed.title, max_length=120)}`.")
        lines.append(f"First embed description: `{summarize_message_text(first_embed.description)}`.")

    forum_entry = extract_forum_catalog_entry(message)
    thread_entry = extract_thread_catalog_entry(message)
    if forum_entry is not None:
        lines.append(
            "Forum parser: "
            f"name=`{forum_entry['name']}` clan=`{forum_entry.get('clan') or 'none'}` "
            f"territories=`{', '.join(forum_entry.get('territories', [])) or 'none'}` "
            f"required_stat=`{forum_entry.get('required_stat') if forum_entry.get('required_stat') is not None else 'none'}`."
        )
    else:
        lines.append("Forum parser: `no match`.")
    if thread_entry is not None:
        lines.append(
            "Thread parser: "
            f"name=`{thread_entry['name']}` "
            f"required_stat=`{thread_entry.get('required_stat') if thread_entry.get('required_stat') is not None else 'none'}`."
        )
    else:
        lines.append("Thread parser: `no match`.")

    await interaction.response.send_message(embed=clean_embed("Message Link Inspect", lines), ephemeral=True)


async def _import_forum_catalog_internal(
    interaction: discord.Interaction,
    forum_post_link: str,
    category: str,
    confirm: bool = False,
    try_link_territories: bool = False,
    weight: app_commands.Range[float, 0.1, 100.0] = 1.0,
    clan_fallback: str | None = None,
    include_stats: bool = True,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    parsed = parse_discord_message_link(forum_post_link)
    if parsed is None:
        await interaction.response.send_message(
            embed=clean_embed("Invalid Link", ["Provide a Discord message link in `forum_post_link`."]),
            ephemeral=True,
        )
        return
    link_guild_id, channel_id, _message_id = parsed
    if link_guild_id != guild.id:
        await interaction.response.send_message(
            embed=clean_embed("Wrong Server", ["That forum post link points to a different server."]),
            ephemeral=True,
        )
        return

    try:
        normalized_category = bot.database.require_category(guild.id, category)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Unknown Category", [str(exc)]), ephemeral=True)
        return

    try:
        channel = await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(
            embed=clean_embed("Import Failed", ["Could not access the linked forum thread."]),
            ephemeral=True,
        )
        return
    if not isinstance(channel, discord.Thread):
        await interaction.response.send_message(
            embed=clean_embed("Invalid Link Target", ["Link must point to a forum thread message."]),
            ephemeral=True,
        )
        return

    fallback_clan = clan_fallback.lower().strip() if clan_fallback else None
    if fallback_clan:
        try:
            bot.database.require_clan(guild.id, fallback_clan)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Unknown Fallback Clan", [str(exc)]), ephemeral=True)
            return

    found_entries: list[dict[str, object]] = []
    omitted_location = 0

    async for message in channel.history(limit=None, oldest_first=True):
        entry = extract_forum_catalog_entry(message)
        if entry is None:
            continue
        entry_clan = str(entry["clan"]).lower() if entry.get("clan") else None
        effective_clan = entry_clan or fallback_clan
        if effective_clan is None:
            omitted_location += 1
            continue
        found_entries.append({**entry, "_effective_clan": effective_clan})

    if not confirm:
        lines = [
            f"Category: `{normalized_category}`.",
            f"Found items: `{len(found_entries)}`.",
            f"Entries omitted due to uncertain location: `{omitted_location}`.",
        ]
        for entry in found_entries[:20]:
            clan_label = str(entry.get("_effective_clan", "?"))
            stat_text = f" +{entry['required_stat']}" if entry.get("required_stat") else ""
            lines.append(f"  • `{entry['name']}`{stat_text} [{clan_label}]")
        if len(found_entries) > 20:
            lines.append(f"  … and {len(found_entries) - 20} more.")
        lines.append("")
        lines.append(":warning: Nothing was imported. Re-run with `confirm: True` to apply.")
        await interaction.response.send_message(embed=clean_embed("Forum Catalog Preview", lines), ephemeral=True)
        return

    imported_items = 0
    linked_clan_rows = 0
    linked_territory_rows = 0
    errors: list[str] = []

    for entry in found_entries:
        try:
            effective_clan = str(entry["_effective_clan"])
            clan_obj = bot.database.require_clan(guild.id, effective_clan)
            item = bot.database.add_item(
                guild.id,
                str(entry["name"]),
                normalized_category,
                enabled=True,
                required_stat=int(entry["required_stat"]) if include_stats and entry.get("required_stat") is not None else None,
            )
            imported_items += 1

            bot.database.set_clan_item_link(guild.id, clan_obj.name, normalized_category, item.name, weight)
            linked_clan_rows += 1

            if try_link_territories:
                for territory_name in entry.get("territories", []):
                    territory_row = bot.database.get_territory(guild.id, str(territory_name))
                    if territory_row is None:
                        continue
                    if territory_row["clan_id"] != clan_obj.id:
                        continue
                    bot.database.set_territory_item_weight(guild.id, str(territory_row["name"]), normalized_category, item.name, weight)
                    linked_territory_rows += 1
        except Exception as exc:
            errors.append(f"{entry.get('name', 'unknown')}: {exc}")

    lines = [
        f"Category: `{normalized_category}`.",
        f"Imported/updated items: `{imported_items}`.",
        f"Clan links applied: `{linked_clan_rows}`.",
        f"Territory links applied: `{linked_territory_rows}`.",
        f"Entries omitted due to uncertain location: `{omitted_location}`.",
    ]
    if errors:
        lines.append(f"Errors: `{len(errors)}`.")
        lines.extend(errors[:10])
    await interaction.response.send_message(embed=clean_embed("Forum Catalog Import", lines), ephemeral=True)


@app_commands.command(name="storage_add", description="Add or remove from a clan's storage.")
@app_commands.describe(
    clan="Clan whose storage to update",
    category="Category of this item",
    item_name="Item to adjust",
    amount="Amount to add (positive) or remove (negative)",
    roll_message_link="Required for positive catches: link to the roll message",
)
async def storage_add(
    interaction: discord.Interaction,
    clan: str,
    category: CategoryLiteral,
    item_name: str,
    amount: int,
    roll_message_link: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "storage_add"):
        return
    guild = interaction.guild
    assert guild is not None
    if not await require_clan_write_access(interaction, bot.database, clan):
        return
    if amount > 0 and roll_message_link is None:
        await interaction.response.send_message(
            embed=clean_embed("Roll Link Required", ["Provide `roll_message_link` when adding catches to storage."]),
            ephemeral=True,
        )
        return
    if roll_message_link is not None and parse_discord_message_link(roll_message_link) is None:
        await interaction.response.send_message(
            embed=clean_embed("Invalid Roll Link", ["Provide a valid Discord message link for `roll_message_link`." ]),
            ephemeral=True,
        )
        return

    if amount > 0:
        try:
            item = bot.database.require_item(guild.id, category, item_name)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Unknown Item", [str(exc)]), ephemeral=True)
            return
        if item.required_stat is not None and item.required_stat_name:
            current_stat = bot.database.get_character_stat(guild.id, interaction.user.id, item.required_stat_name)
            if current_stat is None or current_stat < item.required_stat:
                await interaction.response.send_message(
                    embed=clean_embed(
                        "Stat Requirement Not Met",
                        [
                            f"`{item_name.lower()}` requires `{item.required_stat_name}` >= `{item.required_stat}`.",
                            f"Your current value: `{current_stat if current_stat is not None else 'unset'}`.",
                            "Set stats with `/char_stat_set`.",
                        ],
                    ),
                    ephemeral=True,
                )
                return

    await record_command_event(
        interaction,
        "storage_add",
        clan_name=clan,
        details={"item_name": item_name.lower(), "amount": amount, "roll_message_link": roll_message_link},
    )
    access_level = get_member_access_level(interaction, bot.database)
    quantity = bot.database.adjust_storage(
        guild.id,
        clan,
        category,
        item_name,
        amount,
        source="manual_storage_add",
        metadata={"roll_message_link": roll_message_link} if roll_message_link is not None else None,
        user_id=interaction.user.id,
        access_level=access_level,
    )
    await maybe_send_ratio_alert(bot, guild, clan)
    await interaction.response.send_message(
        embed=clean_embed(
            "Storage Updated",
            [
                f"Clan: `{clan.lower()}`.",
                f"Item: `{item_name.lower()}` ({category}).",
                f"New quantity: `{quantity}`.",
            ],
        )
    )


@app_commands.command(name="char_stat_set", description="Set a character stat value used for item stat-gate checks.")
@app_commands.describe(
    stat_name="Stat name to store (e.g. strength, stealth)",
    value="Integer value for this stat",
    member="Optional member to set the stat for (admin/mod only)",
)
async def char_stat_set(
    interaction: discord.Interaction,
    stat_name: str,
    value: app_commands.Range[int, 0, 1000],
    member: discord.Member | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "char_stat_set"):
        return
    guild = interaction.guild
    assert guild is not None
    target = member or interaction.user
    await record_command_event(
        interaction,
        "char_stat_set",
        details={"target_user_id": target.id, "stat_name": stat_name.lower().strip(), "value": value},
    )
    bot.database.set_character_stat(guild.id, target.id, stat_name, int(value))
    await interaction.response.send_message(
        embed=clean_embed("Character Stat Saved", [f"{target.mention}: `{stat_name.lower().strip()}` = `{value}`."])
    )


@app_commands.command(name="char_stat_show", description="Show character stat values for yourself or another member.")
@app_commands.describe(member="Optional member whose stats you want to view")
async def char_stat_show(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "char_stat_show"):
        return
    guild = interaction.guild
    assert guild is not None
    target = member or interaction.user
    await record_command_event(interaction, "char_stat_show", details={"target_user_id": target.id})
    stats = bot.database.get_character_stats(guild.id, target.id)
    if not stats:
        await interaction.response.send_message(
            embed=clean_embed("No Character Stats", [f"No stats set for {target.mention}."]),
            ephemeral=True,
        )
        return
    lines = [f"`{name}`: `{value}`" for name, value in sorted(stats.items())]
    await interaction.response.send_message(embed=clean_embed(f"Character Stats: {target.display_name}", lines))


@app_commands.command(name="storage_set", description="Set an item's exact quantity in a clan's storage.")
@app_commands.describe(
    clan="Clan whose storage to update",
    category="Category of this item",
    item_name="Item to set",
    amount="Exact quantity to store",
)
async def storage_set(
    interaction: discord.Interaction,
    clan: str,
    category: CategoryLiteral,
    item_name: str,
    amount: app_commands.Range[int, 0, 100000],
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "storage_set"):
        return
    guild = interaction.guild
    assert guild is not None
    if not await require_clan_write_access(interaction, bot.database, clan):
        return
    await record_command_event(interaction, "storage_set", clan_name=clan, details={"item_name": item_name.lower(), "amount": amount})
    access_level = get_member_access_level(interaction, bot.database)
    quantity = bot.database.set_storage(
        guild.id,
        clan,
        category,
        item_name,
        amount,
        source="manual_storage_set",
        user_id=interaction.user.id,
        access_level=access_level,
    )
    await maybe_send_ratio_alert(bot, guild, clan)
    await interaction.response.send_message(
        embed=clean_embed(
            "Storage Set",
            [
                f"Clan: `{clan.lower()}`.",
                f"Item: `{item_name.lower()}` ({category}).",
                f"Quantity set to: `{quantity}`.",
            ],
        )
    )


@app_commands.command(name="storage_show", description="Show a clan's stored item quantities and category totals.")
@app_commands.describe(
    clan="Clan whose storage to view",
    category="Category to filter by; leave blank for all",
)
async def storage_show(interaction: discord.Interaction, clan: str, category: CategoryLiteral | None = None) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "storage_show", clan_name=clan)
    storage = bot.database.get_storage(guild.id, clan, category)
    totals = bot.database.get_category_totals(guild.id, clan)
    clan_record = bot.database.require_clan(guild.id, clan)
    if not storage:
        await interaction.response.send_message(embed=clean_embed("Storage Empty", [f"Clan `{clan_record.name}` has no stored items."]))
        return
    lines = [f"`{item}`: `{quantity}`" for item, quantity in storage.items()]
    ratio_lines: list[str] = []
    if clan_record.cat_count > 0:
        for item_category, total in totals.items():
            ratio_lines.append(f"{item_category} ratio `{total / clan_record.cat_count:.2f}`")
    totals_summary = ", ".join(f"{name} `{count}`" for name, count in sorted(totals.items())) or "none"
    summary_lines = [f"Totals: {totals_summary}"]
    if ratio_lines:
        summary_lines.extend(ratio_lines)
    await interaction.response.send_message(embed=clean_embed(f"Storage: {clan_record.name}", lines + summary_lines))


@app_commands.command(name="roll_forage", description="Roll for finds in a selected category.")
@app_commands.describe(
    category="Category to roll finds from",
    modifier="Bonus or penalty added to the d20 roll (-20 to +20)",
    clan="Clan to store finds in; leave blank to roll without storing",
    territory="Territory to use for weighted drop table; leave blank to auto-detect from this channel/thread",
    stat="Optional stat value used for stat-gated items",
)
async def roll_forage(
    interaction: discord.Interaction,
    category: CategoryLiteral,
    modifier: app_commands.Range[int, -20, 20] = 0,
    clan: str | None = None,
    territory: str | None = None,
    stat: app_commands.Range[int, 0, 1000] | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "roll_forage"):
        return
    guild = interaction.guild
    assert guild is not None
    if clan and not await require_clan_write_access(interaction, bot.database, clan):
        return
    selected_territory = territory.lower() if territory else None
    auto_detected = False
    if selected_territory is None:
        selected_territory = resolve_territory_from_context(
            interaction,
            bot.database,
            allow_name_fallback=True,
        )
        auto_detected = selected_territory is not None

    await record_command_event(
        interaction,
        "roll_forage",
        clan_name=clan,
        details={"territory": selected_territory, "auto_detected": auto_detected, "stat": stat},
    )
    try:
        access_level = get_member_access_level(interaction, bot.database)
        result = bot.roll_service.forage(
            guild.id,
            category=category,
            modifier=modifier,
            clan_name=clan,
            territory_name=selected_territory,
            stat=stat,
            actor_user_id=interaction.user.id,
            actor_access_level=access_level,
        )
    except ValueError as error:
        await interaction.response.send_message(embed=clean_embed("Roll Failed", [str(error)]), ephemeral=True)
        return
    if clan:
        await maybe_send_ratio_alert(bot, guild, clan)
    result_embed = format_forage_result(result)
    await interaction.response.send_message(embed=result_embed)
    if clan:
        clan_obj = bot.database.get_clan(guild.id, clan)
        if clan_obj and clan_obj.roll_log_channel_id is not None:
            log_channel = guild.get_channel(clan_obj.roll_log_channel_id)
            if isinstance(log_channel, discord.TextChannel):
                await log_channel.send(embed=result_embed)


@app_commands.command(name="test_roll_forage", description="Simulate a forage roll without changing storage.")
@app_commands.describe(
    category="Category to roll finds from",
    modifier="Bonus or penalty added to the d20 roll (-20 to +20)",
    clan="Optional clan label to preview the result against",
    territory="Territory to use for weighted drop table; leave blank to auto-detect from this channel/thread",
    stat="Optional stat value used for stat-gated items",
)
async def test_roll_forage(
    interaction: discord.Interaction,
    category: CategoryLiteral,
    modifier: app_commands.Range[int, -20, 20] = 0,
    clan: str | None = None,
    territory: str | None = None,
    stat: app_commands.Range[int, 0, 1000] | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "test_roll_forage"):
        return
    guild = interaction.guild
    assert guild is not None
    selected_territory = territory.lower() if territory else None
    auto_detected = False
    if selected_territory is None:
        selected_territory = resolve_territory_from_context(
            interaction,
            bot.database,
            allow_name_fallback=True,
        )
        auto_detected = selected_territory is not None

    await record_command_event(
        interaction,
        "test_roll_forage",
        clan_name=clan,
        details={"territory": selected_territory, "auto_detected": auto_detected, "stat": stat},
    )
    try:
        if clan:
            bot.database.require_clan(guild.id, clan)
        result = bot.roll_service.forage(
            guild.id,
            category=category,
            modifier=modifier,
            clan_name=clan,
            territory_name=selected_territory,
            stat=stat,
            store_results=False,
        )
    except ValueError as error:
        await interaction.response.send_message(embed=clean_embed("Test Roll Failed", [str(error)]), ephemeral=True)
        return
    await interaction.response.send_message(embed=format_forage_result(result, dry_run=True))


@app_commands.command(name="roll", description="Roll a plain d20 or d100 with optional mod/buff/debuff.")
@app_commands.describe(
    die="Choose the die to roll",
    mod="Base modifier added to the roll (can be negative)",
    buff="Optional buff bonus added to the total",
    debuff="Optional debuff penalty subtracted from the total",
    clan="Optional clan used to mirror this roll into that clan's log channel",
)
async def roll(
    interaction: discord.Interaction,
    die: Literal["d20", "d100"] = "d20",
    mod: app_commands.Range[int, -1000, 1000] = 0,
    buff: app_commands.Range[int, 0, 1000] = 0,
    debuff: app_commands.Range[int, 0, 1000] = 0,
    clan: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "roll"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    sides = 20 if die == "d20" else 100
    roll_value = bot.roll_service.rng.randint(1, sides)
    total = roll_value + mod + buff - debuff

    detail_parts = [f"{die} `{roll_value}`", f"with mod `{mod:+d}`"]
    if buff > 0:
        detail_parts.append(f"with buff `+{buff}`")
    if debuff > 0:
        detail_parts.append(f"with debuff `-{debuff}`")
    detail = " ".join(detail_parts) + f" = total `{total}`"

    await record_command_event(
        interaction,
        "roll",
        clan_name=clan,
        details={"die": die, "rolled": roll_value, "mod": mod, "buff": buff, "debuff": debuff, "total": total},
    )
    result_embed = clean_embed(
        "Roll Result",
        [
            detail,
            f"Formula: `{roll_value}` + `{mod}` + `{buff}` - `{debuff}` = `{total}`",
        ],
    )
    await interaction.response.send_message(embed=result_embed)
    if clan:
        clan_obj = bot.database.get_clan(guild.id, clan)
        if clan_obj and clan_obj.roll_log_channel_id is not None:
            log_channel = guild.get_channel(clan_obj.roll_log_channel_id)
            if isinstance(log_channel, discord.TextChannel):
                await log_channel.send(embed=result_embed)


@app_commands.command(name="roll_config_show", description="Show current forage roll total bands and find counts.")
async def roll_config_show(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "roll_config_show"):
        return
    guild = interaction.guild
    assert guild is not None
    ranges = bot.database.list_roll_ranges(guild.id)
    lines = [f"`{minimum}` to `{maximum}` => `{dose}` finds" for minimum, maximum, dose in ranges]
    await record_command_event(interaction, "roll_config_show")
    await interaction.response.send_message(embed=clean_embed("Roll Ranges", lines))


@app_commands.command(name="roll_config_set", description="Set custom forage roll total bands.")
@app_commands.describe(
    ranges="Comma-separated bands, e.g. 0-5:0, 6-10:1, 11-15:2, 16-20:3, 21-999:4",
)
async def roll_config_set(interaction: discord.Interaction, ranges: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "roll_config_set"):
        return
    guild = interaction.guild
    assert guild is not None
    try:
        parsed_ranges = parse_roll_ranges_input(ranges)
        bot.database.set_roll_ranges(guild.id, parsed_ranges)
    except ValueError as error:
        await interaction.response.send_message(
            embed=clean_embed(
                "Invalid Roll Config",
                [
                    str(error),
                    "Expected format: `min-max:dose` entries separated by commas.",
                ],
            ),
            ephemeral=True,
        )
        return
    await record_command_event(interaction, "roll_config_set", details={"ranges": ranges})
    lines = [f"`{minimum}` to `{maximum}` => `{dose}` finds" for minimum, maximum, dose in bot.database.list_roll_ranges(guild.id)]
    await interaction.response.send_message(embed=clean_embed("Roll Config Updated", lines))


@app_commands.command(name="roll_config_reset", description="Reset forage roll bands to defaults.")
async def roll_config_reset(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "roll_config_reset"):
        return
    guild = interaction.guild
    assert guild is not None
    bot.database.reset_roll_ranges(guild.id)
    await record_command_event(interaction, "roll_config_reset")
    lines = [f"`{minimum}` to `{maximum}` => `{dose}` finds" for minimum, maximum, dose in bot.database.list_roll_ranges(guild.id)]
    await interaction.response.send_message(embed=clean_embed("Roll Config Reset", lines))


@app_commands.command(name="preview_thread_link", description="Resolve a Discord thread link and report what the bot can read.")
@app_commands.describe(link="Discord thread link or thread ID")
async def preview_thread_link(interaction: discord.Interaction, link: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "preview_thread_link"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    try:
        thread = await resolve_thread_from_link(guild, link)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Inspect Failed", [str(exc)]), ephemeral=True)
        return

    await record_command_event(
        interaction,
        "preview_thread_link",
        details={"link": link, "thread_id": thread.id, "parent_id": thread.parent_id},
    )

    lines = [
        f"Thread: `{thread.name}` (`{thread.id}`).",
        f"Parent: `{getattr(thread.parent, 'name', 'unknown')}` (`{thread.parent_id}`).",
        f"Archived: `{thread.archived}`.",
        f"Locked: `{thread.locked}`.",
        f"Message count hint: `{thread.message_count if thread.message_count is not None else 'unknown'}`.",
    ]

    if isinstance(thread.parent, discord.ForumChannel):
        try:
            active_count, archived_count, archived_complete = await count_forum_threads_breakdown(thread.parent)
            lines.append(f"Forum tracking active threads: `{active_count}`.")
            if archived_complete:
                lines.append(f"Forum tracking archived threads: `{archived_count}`.")
            else:
                lines.append("Forum tracking archived threads: `unavailable` (missing permission or API access).")
            lines.append(f"Forum tracking total threads counted: `{active_count + archived_count}`.")
        except Exception as exc:
            lines.append(f"Forum tracking count failed: `{exc}`.")

    starter_message = thread.starter_message
    if starter_message is None:
        try:
            starter_message = await thread.fetch_message(thread.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            starter_message = None

    if starter_message is None:
        lines.append("Starter message: `not accessible`.")
        await interaction.response.send_message(embed=clean_embed("Thread Link Inspect", lines), ephemeral=True)
        return

    lines.append(f"Starter message ID: `{starter_message.id}`.")
    lines.append(f"Starter preview: `{summarize_message_text(starter_message.content)}`.")
    lines.append(f"Starter embeds: `{len(starter_message.embeds)}`.")

    thread_entry = extract_thread_catalog_entry(starter_message)
    if thread_entry is not None:
        lines.append(
            "Thread parser: "
            f"name=`{thread_entry['name']}` "
            f"required_stat=`{thread_entry.get('required_stat') if thread_entry.get('required_stat') is not None else 'none'}`."
        )
    else:
        lines.append("Thread parser: `no match`.")

    lines.append("Thread import parser expects first line format: `➺・item-name―+12` (stat is optional).")

    found_items: list[dict[str, object]] = []
    unmatched_samples: list[str] = []
    try:
        async for msg in thread.history(limit=20, oldest_first=True):
            entry = extract_thread_catalog_entry(msg)
            if entry is not None:
                found_items.append(entry)
                continue
            raw_first_line = (msg.content or "").strip().splitlines()
            if raw_first_line:
                preview_line = summarize_message_text(raw_first_line[0], max_length=90)
                if preview_line and len(unmatched_samples) < 3:
                    unmatched_samples.append(preview_line)
    except (discord.Forbidden, discord.HTTPException) as exc:
        lines.append(f"Item scan failed: `{exc}`.")
    if found_items:
        lines.append(f"Importable items found: `{len(found_items)}`.")
        for item_entry in found_items[:5]:
            stat_text = f" +{item_entry['required_stat']}" if item_entry.get("required_stat") is not None else ""
            lines.append(f"  • `{item_entry['name']}`{stat_text}")
        if len(found_items) > 5:
            lines.append(f"  … and {len(found_items) - 5} more (scanned first 20 messages).")
    else:
        lines.append("Importable items: `none found`.")
    if unmatched_samples:
        lines.append("Examples that did not match thread import format:")
        for sample in unmatched_samples:
            lines.append(f"  • `{sample}`")

    await interaction.response.send_message(embed=clean_embed("Thread Link Inspect", lines), ephemeral=True)


@app_commands.command(name="preview_forum_count", description="Show exactly how many threads the bot can count in a forum.")
@app_commands.describe(link="Discord forum link, thread link in that forum, or forum/thread ID")
async def preview_forum_count(interaction: discord.Interaction, link: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "preview_forum_count"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    try:
        forum_channel = await resolve_forum_channel_from_link(guild, link)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Preview Failed", [str(exc)]), ephemeral=True)
        return

    active_count, archived_count, archived_complete = await count_forum_threads_breakdown(forum_channel)
    total_count = active_count + archived_count
    await record_command_event(
        interaction,
        "preview_forum_count",
        details={
            "link": link,
            "forum_id": forum_channel.id,
            "active_count": active_count,
            "archived_count": archived_count,
            "archived_complete": archived_complete,
            "total": total_count,
        },
    )

    lines = [
        f"Forum: `{forum_channel.name}` (`{forum_channel.id}`).",
        f"Active threads counted: `{active_count}`.",
        f"Archived threads counted: `{archived_count}`.",
        f"Total counted threads: `{total_count}`.",
    ]
    if not archived_complete:
        lines.append(":warning: Archived thread access is unavailable, so total may be lower than the real forum total.")
    await interaction.response.send_message(embed=clean_embed("Forum Count Preview", lines), ephemeral=True)


@app_commands.command(name="alert_config", description="Configure storage ratio alerts for a clan.")
@app_commands.describe(
    clan="Clan to configure alerts for",
    enabled="Enable or disable storage alerts",
    alert_channel="Channel where alert messages will be posted",
)
async def alert_config(
    interaction: discord.Interaction,
    clan: str,
    enabled: bool,
    alert_channel: discord.TextChannel,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "alert_config"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "alert_config", clan_name=clan)
    updated = bot.database.update_clan_config(
        guild.id,
        clan,
        alerts_enabled=enabled,
        alert_channel_id=alert_channel.id,
    )
    await interaction.response.send_message(
        embed=clean_embed(
            "Alerts Updated",
            [
                f"Clan: `{updated.name}`.",
                f"Enabled: `{updated.alerts_enabled}`.",
                f"Channel: {alert_channel.mention}.",
                "Use `/category_threshold_set` to manage per-category threshold values.",
            ],
        )
    )


@app_commands.command(name="permission_set", description="Set role or user access tier.")
@app_commands.describe(
    access_level="Access tier to assign (built-in or custom)",
    role="Discord role to map to this access tier",
    user="Discord user to map to this access tier",
)
async def permission_set(
    interaction: discord.Interaction,
    access_level: str,
    role: discord.Role | None = None,
    user: discord.Member | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "permission_set"):
        return
    guild = interaction.guild
    assert guild is not None
    normalized_access = access_level.strip().lower()
    if normalized_access not in bot.database.get_access_level_ranks(guild.id):
        await interaction.response.send_message(
            embed=clean_embed("Unknown Access Level", [f"`{normalized_access}` is not defined in this server."]),
            ephemeral=True,
        )
        return
    if role is None and user is None:
        await interaction.response.send_message(
            embed=clean_embed("Missing Target", ["Provide either `role` or `user`."]),
            ephemeral=True,
        )
        return
    if role is not None and user is not None:
        await interaction.response.send_message(
            embed=clean_embed("Choose One Target", ["Provide only one target: `role` or `user`."]),
            ephemeral=True,
        )
        return

    if role is not None:
        await record_command_event(interaction, "permission_set", details={"target_access": normalized_access, "role_id": role.id})
        bot.database.set_role_permission(guild.id, normalized_access, role.id)
        await interaction.response.send_message(
            embed=clean_embed("Role Permission Added", [f"{role.mention} now maps to `{normalized_access}` access."])
        )
        return

    assert user is not None
    await record_command_event(interaction, "permission_set", details={"target_access": normalized_access, "user_id": user.id})
    bot.database.set_user_permission(guild.id, normalized_access, user.id)
    await interaction.response.send_message(
        embed=clean_embed("User Permission Added", [f"{user.mention} now maps to `{normalized_access}` access."])
    )


@app_commands.command(name="permission_show", description="Show configured access mappings for roles and users.")
async def permission_show(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "permission_show")
    role_map = bot.database.get_role_permissions(guild.id)
    user_map = bot.database.get_user_permissions(guild.id)
    if not role_map and not user_map:
        await interaction.response.send_message(
            embed=clean_embed(
                "Permissions",
                [
                    "No custom role or user permissions configured.",
                    "Admin still follows Discord Administrator permission.",
                    "Mod still follows Discord Manage Server permission.",
                ],
            )
        )
        return

    def format_role_mentions(role_ids: list[int]) -> str:
        if not role_ids:
            return "none"
        mentions: list[str] = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                mentions.append(role.mention)
            else:
                # Fallback mention still renders in Discord even if local cache is stale.
                mentions.append(f"<@&{role_id}>")
        return ", ".join(mentions)

    def format_user_mentions(user_ids: list[int]) -> str:
        if not user_ids:
            return "none"
        return ", ".join(f"<@{user_id}>" for user_id in user_ids)

    lines: list[str] = []
    level_names = [name for name, _, _ in bot.database.list_access_levels(guild.id)]
    for level in level_names:
        lines.append(
            f"`{level}` roles: {format_role_mentions(role_map.get(level, []))} | users: {format_user_mentions(user_map.get(level, []))}"
        )
    await interaction.response.send_message(embed=clean_embed("Permissions", lines))


@app_commands.command(name="access_level_create", description="Create or update a custom access level.")
@app_commands.describe(
    name="Level name (lowercase letters/numbers/underscore)",
    rank="Numeric rank (1-299): higher rank has more permission",
)
async def access_level_create(
    interaction: discord.Interaction,
    name: str,
    rank: app_commands.Range[int, 1, 299],
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "access_level_create"):
        return
    guild = interaction.guild
    assert guild is not None
    try:
        normalized = bot.database.create_access_level(guild.id, name, int(rank))
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Access Level Error", [str(exc)]), ephemeral=True)
        return
    await record_command_event(interaction, "access_level_create", details={"name": normalized, "rank": int(rank)})
    await interaction.response.send_message(
        embed=clean_embed(
            "Access Level Saved",
            [
                f"Level: `{normalized}`.",
                f"Rank: `{int(rank)}`.",
            ],
        )
    )


@app_commands.command(name="access_level_remove", description="Remove a custom access level.")
@app_commands.describe(
    name="Custom level name to remove",
)
async def access_level_remove(interaction: discord.Interaction, name: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "access_level_remove"):
        return
    guild = interaction.guild
    assert guild is not None
    try:
        removed = bot.database.remove_access_level(guild.id, name)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Access Level Error", [str(exc)]), ephemeral=True)
        return
    normalized = name.strip().lower()
    await record_command_event(interaction, "access_level_remove", details={"name": normalized, "removed": removed})
    if removed:
        await interaction.response.send_message(
            embed=clean_embed("Access Level Removed", [f"Removed custom level `{normalized}` and related mappings."])
        )
    else:
        await interaction.response.send_message(
            embed=clean_embed("No Change", [f"Custom level `{normalized}` does not exist."])
        )


@app_commands.command(name="access_level_list", description="List built-in and custom access levels.")
async def access_level_list(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "access_level_list"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "access_level_list")
    levels = bot.database.list_access_levels(guild.id)
    lines = [
        f"`{name}` rank `{rank}`{' (built-in)' if is_builtin else ''}"
        for name, rank, is_builtin in levels
    ]
    await interaction.response.send_message(embed=clean_embed("Access Levels", lines))


@app_commands.command(name="use_item", description="Consume an item from a clan's storage.")
@app_commands.describe(
    category="Category of the item being consumed",
    item_name="Name of the item being consumed",
    clan="Clan whose storage will be reduced",
    amount="Amount to consume from storage (default: 1)",
)
async def use_item(
    interaction: discord.Interaction,
    category: str,
    item_name: str,
    clan: str,
    amount: app_commands.Range[int, 1, 100] = 1,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "use_item"):
        return
    guild = interaction.guild
    assert guild is not None
    if not await require_clan_write_access(interaction, bot.database, clan):
        return
    await record_command_event(
        interaction,
        "use_item",
        clan_name=clan,
        details={"category": category.lower(), "item_name": item_name.lower(), "amount": amount},
    )
    try:
        bot.database.require_category(guild.id, category)
        access_level = get_member_access_level(interaction, bot.database)
        quantity = bot.database.adjust_storage(
            guild.id,
            clan,
            category,
            item_name,
            -amount,
            source="use_item",
            user_id=interaction.user.id,
            access_level=access_level,
        )
    except ValueError as error:
        await interaction.response.send_message(embed=clean_embed("Use Item Failed", [str(error)]), ephemeral=True)
        return
    await maybe_send_ratio_alert(bot, guild, clan)
    await interaction.response.send_message(
        embed=clean_embed(
            "Item Consumed",
            [
                f"Clan: `{clan.lower()}`.",
                f"Item: `{item_name.lower()}` ({category.lower()}).",
                f"Amount used: `{amount}`.",
                f"Remaining: `{quantity}`.",
            ],
        )
    )




@app_commands.command(name="impersonate_access", description="Temporarily view the bot as a lower access tier.")
@app_commands.describe(
    access_level="Lower access tier to impersonate; leave blank to clear",
)
async def impersonate_access(interaction: discord.Interaction, access_level: str | None = None) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return
    await record_command_event(interaction, "impersonate_access", details={"target_access": access_level})

    base_level = get_base_member_access_level(interaction, bot.database)
    if not access_allows(bot.database, guild.id, base_level, "mod"):
        await interaction.response.send_message(
            embed=clean_embed("Access Denied", ["Only admins and mods can impersonate lower access levels."]),
            ephemeral=True,
        )
        return

    if access_level is None:
        cleared = bot.database.clear_access_impersonation(guild.id, interaction.user.id)
        if cleared:
            await interaction.response.send_message(
                embed=clean_embed("Impersonation Cleared", ["Your normal access is active again."]),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                embed=clean_embed("No Impersonation", ["There is no active impersonation to clear."]),
                ephemeral=True,
            )
        return

    target_level = access_level.strip().lower()
    levels = bot.database.get_access_level_ranks(guild.id)
    if target_level not in levels:
        await interaction.response.send_message(
            embed=clean_embed("Invalid Target", [f"Unknown access level: `{target_level}`."]),
            ephemeral=True,
        )
        return
    if levels[target_level] >= levels.get(base_level, 0):
        await interaction.response.send_message(
            embed=clean_embed(
                "Invalid Target",
                [f"You can only impersonate levels lower than your own `{base_level}` access."],
            ),
            ephemeral=True,
        )
        return

    bot.database.set_access_impersonation(guild.id, interaction.user.id, target_level)
    await interaction.response.send_message(
        embed=clean_embed(
            "Impersonation Active",
            [
                f"You are now acting as `{target_level}`.",
                "Run `/impersonate_access` with no access level to clear.",
            ],
        ),
        ephemeral=True,
    )


@app_commands.command(name="command_access_set", description="Override the required access tier for a command.")
@app_commands.describe(
    command_name="Bot command to override (e.g. storage_add)",
    access_level="New minimum access tier required to run this command",
)
async def command_access_set(interaction: discord.Interaction, command_name: str, access_level: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "command_access_set"):
        return
    guild = interaction.guild
    assert guild is not None
    normalized_access = access_level.strip().lower()
    await record_command_event(interaction, "command_access_set", details={"command_name": command_name.lower(), "target_access": normalized_access})

    normalized_name = command_name.strip().lower()
    if normalized_name not in COMMAND_ACCESS:
        valid = ", ".join(sorted(COMMAND_ACCESS.keys()))
        await interaction.response.send_message(
            embed=clean_embed("Unknown Command", [f"`{normalized_name}` is not a valid command.", f"Valid commands: {valid}"]),
            ephemeral=True,
        )
        return

    if normalized_access not in bot.database.get_access_level_ranks(guild.id):
        await interaction.response.send_message(
            embed=clean_embed("Unknown Access Level", [f"`{normalized_access}` is not defined in this server."]),
            ephemeral=True,
        )
        return

    bot.database.set_command_access_override(guild.id, normalized_name, normalized_access)
    await interaction.response.send_message(
        embed=clean_embed("Command Access Updated", [f"`{normalized_name}` now requires `{normalized_access}` access."])
    )


@app_commands.command(name="command_access_reset", description="Clear a command access override and restore the default.")
@app_commands.describe(
    command_name="Bot command to reset to its built-in default",
)
async def command_access_reset(interaction: discord.Interaction, command_name: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "command_access_reset"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "command_access_reset", details={"command_name": command_name.lower()})

    normalized_name = command_name.strip().lower()
    if normalized_name not in COMMAND_ACCESS:
        valid = ", ".join(sorted(COMMAND_ACCESS.keys()))
        await interaction.response.send_message(
            embed=clean_embed("Unknown Command", [f"`{normalized_name}` is not a valid command.", f"Valid commands: {valid}"]),
            ephemeral=True,
        )
        return

    removed = bot.database.clear_command_access_override(guild.id, normalized_name)
    default_level = COMMAND_ACCESS[normalized_name]
    if removed:
        await interaction.response.send_message(
            embed=clean_embed("Command Access Reset", [f"`{normalized_name}` reverted to default `{default_level}` access."])
        )
    else:
        await interaction.response.send_message(
            embed=clean_embed("No Change", [f"`{normalized_name}` already uses default `{default_level}` access."])
        )


@app_commands.command(name="command_access_show", description="Show effective required access levels for all commands.")
async def command_access_show(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "command_access_show"):
        return
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "command_access_show")

    overrides = bot.database.get_command_access_overrides(guild.id)
    lines: list[str] = []
    for command_name in sorted(COMMAND_ACCESS.keys()):
        default_level = COMMAND_ACCESS[command_name]
        effective_level = overrides.get(command_name, default_level)
        if command_name in overrides:
            lines.append(f"`{command_name}`: `{effective_level}` (default `{default_level}`)")
        else:
            lines.append(f"`{command_name}`: `{effective_level}`")
    await interaction.response.send_message(embed=clean_embed("Command Access", lines))


@app_commands.command(name="config_show", description="Show clan config details or the full command access overview.")
@app_commands.describe(
    clan="Clan to show in detail; leave blank to see the command access overview",
)
async def config_show(interaction: discord.Interaction, clan: str | None = None) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    assert guild is not None
    await record_command_event(interaction, "config_show", clan_name=clan)
    if clan:
        clan_record = bot.database.require_clan(guild.id, clan)
        embed = clean_embed(
            f"Config: {clan_record.name}",
            [
                f"Tracking: `{clan_record.tracking_mode}`.",
                f"Cats: `{clan_record.cat_count}`.",
                f"Link: `{clan_record.tracking_link or 'none'}`.",
                f"Alerts enabled: `{clan_record.alerts_enabled}`.",
                f"Territory required by default (global): `{bot.database.get_global_territory_requirement(guild.id)}`.",
            ],
        )
        await interaction.response.send_message(embed=embed)
        return

    overrides = bot.database.get_command_access_overrides(guild.id)
    access_lines: list[str] = []
    for command_name in sorted(COMMAND_ACCESS.keys()):
        default_level = COMMAND_ACCESS[command_name]
        effective_level = overrides.get(command_name, default_level)
        if command_name in overrides:
            access_lines.append(f"`{command_name}`: `{effective_level}` (default `{default_level}`)")
        else:
            access_lines.append(f"`{command_name}`: `{effective_level}`")
    await interaction.response.send_message(embed=clean_embed("Command Access Levels", access_lines))


@app_commands.command(name="audit_log_show", description="View recent storage changes and commands for a clan.")
@app_commands.describe(
    clan="Optional clan to filter audit entries",
    limit="Maximum number of entries to show",
)
async def audit_log_show(
    interaction: discord.Interaction,
    clan: str | None = None,
    limit: app_commands.Range[int, 1, 50] = 15,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "audit_log_show"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    viewer_access = get_member_access_level(interaction, bot.database)
    visible_clans = get_visible_clan_names(interaction, bot.database)
    if not can_manage_all_clans(guild.id, bot.database, viewer_access) and clan and clan.lower() not in visible_clans:
        await interaction.response.send_message(
            embed=clean_embed("Clan Access Required", [f"You cannot audit `{clan.lower()}`."]),
            ephemeral=True,
        )
        return

    await record_command_event(interaction, "audit_log_show", clan_name=clan, details={"limit": limit})
    entries = bot.database.get_audit_entries(
        guild.id,
        viewer_access_level=viewer_access,
        visible_clans=None if can_manage_all_clans(guild.id, bot.database, viewer_access) else visible_clans,
        clan_name=clan,
        limit=limit,
    )
    if not entries:
        await interaction.response.send_message(embed=clean_embed("Audit Log", ["No audit entries matched this filter."]), ephemeral=True)
        return

    lines = ["Retention: audit records are kept for 31 days.", *[format_audit_entry(entry, guild) for entry in entries]]
    embeds = chunk_lines_for_embed("Audit Log", lines)
    await interaction.response.send_message(embed=embeds[0], ephemeral=True)
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=True)


async def _import_thread_catalog_internal(
    interaction: discord.Interaction,
    thread: str,
    category: str,
    confirm: bool = False,
    import_items: str | None = None,
    weight: app_commands.Range[float, 0.1, 100.0] = 1.0,
    clan_fallback: str | None = None,
    include_stats: bool = True,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    try:
        target_thread = await resolve_thread_from_link(guild, thread)
    except ValueError:
        await interaction.response.send_message(
            embed=clean_embed(
                "Thread Not Found",
                [
                    "Could not find a thread matching the provided ID or link.",
                    "Provide a thread ID or a Discord thread link in `thread`.",
                ],
            ),
            ephemeral=True,
        )
        return

    try:
        normalized_category = bot.database.require_category(guild.id, category)
    except ValueError as exc:
        await interaction.response.send_message(embed=clean_embed("Unknown Category", [str(exc)]), ephemeral=True)
        return

    fallback_clan: str | None = None
    if clan_fallback:
        fallback_clan = clan_fallback.lower().strip()
        try:
            bot.database.require_clan(guild.id, fallback_clan)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Unknown Fallback Clan", [str(exc)]), ephemeral=True)
            return

    entries: list[dict[str, object]] = []
    async for message in target_thread.history(limit=None, oldest_first=True):
        entry = extract_thread_catalog_entry(message)
        if entry is not None:
            entries.append(entry)

    if not confirm:
        if not entries:
            await interaction.response.send_message(
                embed=clean_embed("Thread Catalog Preview", ["No items found in thread."]),
                ephemeral=True,
            )
            return
        lines = [f"Thread: `{target_thread.name}`.", f"Category: `{normalized_category}`."]
        lines.append(f"Found `{len(entries)}` items:")
        for entry in entries[:20]:
            stat_text = f" +{entry['required_stat']}" if entry.get("required_stat") else ""
            lines.append(f"  • `{entry['name']}`{stat_text}")
        if len(entries) > 20:
            lines.append(f"  … and {len(entries) - 20} more.")
        lines.append("")
        lines.append(":warning: Nothing was imported. Re-run with `confirm: True` to apply.")
        await interaction.response.send_message(embed=clean_embed("Thread Catalog Preview", lines), ephemeral=True)
        return

    import_list = entries
    if import_items:
        import_names_lower = {name.lower().strip() for name in import_items.split(",")}
        import_list = [e for e in entries if str(e["name"]).lower() in import_names_lower]
        if not import_list:
            await interaction.response.send_message(
                embed=clean_embed("No Matching Items", ["None of the specified items were found in the thread."]),
                ephemeral=True,
            )
            return

    imported_count = 0
    linked_clan_rows = 0
    errors: list[str] = []

    for entry in import_list:
        try:
            item = bot.database.add_item(
                guild.id,
                str(entry["name"]),
                normalized_category,
                enabled=True,
                required_stat=int(entry["required_stat"]) if include_stats and entry.get("required_stat") is not None else None,
            )
            imported_count += 1
            if fallback_clan:
                clan_obj = bot.database.require_clan(guild.id, fallback_clan)
                bot.database.set_clan_item_link(guild.id, clan_obj.name, normalized_category, item.name, weight)
                linked_clan_rows += 1
        except Exception as exc:
            errors.append(f"{entry.get('name', 'unknown')}: {exc}")

    lines = [
        f"Thread: `{target_thread.name}`.",
        f"Category: `{normalized_category}`.",
        f"Imported/updated items: `{imported_count}`.",
        f"Clan links applied: `{linked_clan_rows}`.",
    ]
    if errors:
        lines.append(f"Errors: `{len(errors)}`.")
        lines.extend(errors[:10])
    await interaction.response.send_message(embed=clean_embed("Thread Catalog Imported", lines), ephemeral=True)


@app_commands.command(name="audit_export_json", description="Download audit entries as JSON for a selected timeframe.")
@app_commands.describe(
    since_days="How many days of audit history to export (max 31)",
    clan="Optional clan filter",
)
async def audit_export_json(
    interaction: discord.Interaction,
    since_days: app_commands.Range[int, 1, 31] = 31,
    clan: str | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "audit_export_json"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    if clan is not None:
        try:
            bot.database.require_clan(guild.id, clan)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Unknown Clan", [str(exc)]), ephemeral=True)
            return

    payload = bot.database.export_audit_entries(guild.id, since_days=since_days, clan_name=clan)
    await record_command_event(
        interaction,
        "audit_export_json",
        clan_name=clan,
        details={"since_days": since_days, "storage_rows": len(payload["storage_entries"]), "command_rows": len(payload["command_entries"])}
    )

    blob = json.dumps(payload, indent=2).encode("utf-8")
    filename = f"audit_export_{guild.id}_{since_days}d.json"
    file = discord.File(io.BytesIO(blob), filename=filename)
    await interaction.response.send_message(
        content=f"Exported audit data for the last {since_days} days.",
        file=file,
        ephemeral=True,
    )


@app_commands.command(name="audit_clear", description="Delete audit entries, optionally filtered by clan/category/territory.")
@app_commands.describe(
    clan="Optional clan filter",
    category="Optional category filter",
    territory="Optional territory filter",
    confirm="Set to true to confirm deletion",
)
async def audit_clear(
    interaction: discord.Interaction,
    clan: str | None = None,
    category: str | None = None,
    territory: str | None = None,
    confirm: bool = False,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "audit_clear"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    if not confirm:
        await interaction.response.send_message(
            embed=clean_embed(
                "Confirmation Required",
                [
                    "`/audit_clear` permanently deletes audit records.",
                    "Re-run the command with `confirm:true` after setting any clan/category/territory filters you want.",
                ],
            ),
            ephemeral=True,
        )
        return

    if clan is not None:
        try:
            bot.database.require_clan(guild.id, clan)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Unknown Clan", [str(exc)]), ephemeral=True)
            return
    if category is not None:
        try:
            bot.database.require_category(guild.id, category)
        except ValueError as exc:
            await interaction.response.send_message(embed=clean_embed("Unknown Category", [str(exc)]), ephemeral=True)
            return
    if territory is not None and bot.database.get_territory(guild.id, territory) is None:
        await interaction.response.send_message(
            embed=clean_embed("Unknown Territory", [f"Territory `{territory.lower()}` was not found."]),
            ephemeral=True,
        )
        return

    deleted = bot.database.clear_audit_entries(
        guild.id,
        clan_name=clan,
        category=category,
        territory_name=territory,
    )
    await record_command_event(
        interaction,
        "audit_clear",
        clan_name=clan,
        details={
            "category": deleted["category"],
            "territory": deleted["territory_name"],
            "storage_deleted": deleted["storage_deleted"],
            "command_deleted": deleted["command_deleted"],
            "total_deleted": deleted["total_deleted"],
        },
    )

    await interaction.response.send_message(
        embed=clean_embed(
            "Audit Cleared",
            [
                f"Clan filter: `{deleted['clan_name'] or 'none'}`.",
                f"Category filter: `{deleted['category'] or 'none'}`.",
                f"Territory filter: `{deleted['territory_name'] or 'none'}`.",
                f"Storage entries deleted: `{deleted['storage_deleted']}`.",
                f"Command entries deleted: `{deleted['command_deleted']}`.",
                f"Total deleted: `{deleted['total_deleted']}`.",
            ],
        ),
        ephemeral=True,
    )


@app_commands.command(name="audit_undo_last", description="Undo the latest eligible storage change for a clan.")
@app_commands.describe(
    clan="Clan whose latest storage action should be undone",
)
async def audit_undo_last(interaction: discord.Interaction, clan: str) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "audit_undo_last"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return
    if not await require_clan_write_access(interaction, bot.database, clan):
        return

    viewer_access = get_member_access_level(interaction, bot.database)
    visible_clans = None if can_manage_all_clans(guild.id, bot.database, viewer_access) else get_visible_clan_names(interaction, bot.database)
    undone = bot.database.undo_latest_storage_entry(
        guild.id,
        viewer_access_level=viewer_access,
        viewer_user_id=interaction.user.id,
        clan_name=clan,
        visible_clans=visible_clans,
        actor_user_id=interaction.user.id,
        actor_access_level=viewer_access,
    )
    if undone is None:
        await interaction.response.send_message(
            embed=clean_embed("Undo Unavailable", [f"No eligible storage event was found for `{clan.lower()}`."]),
            ephemeral=True,
        )
        return

    await record_command_event(
        interaction,
        "audit_undo_last",
        clan_name=clan,
        details={
            "undid_history_id": int(undone["id"]),
            "item_name": str(undone["item_name"]),
            "delta": int(undone["delta"]),
        },
    )
    await maybe_send_ratio_alert(bot, guild, clan)
    await interaction.response.send_message(
        embed=clean_embed(
            "Undo Complete",
            [
                f"Clan: `{str(undone['clan_name']).lower()}`.",
                f"Item: `{str(undone['item_name']).lower()}` ({str(undone['category'])}).",
                f"Reversed delta: `{int(undone['delta'])}` from `{str(undone['source'])}`.",
                f"New quantity: `{int(undone['new_quantity'])}`.",
            ],
        )
    )


@app_commands.command(name="dashboard_show", description="Show a clan or server-wide storage dashboard.")
@app_commands.describe(
    clan="Optional clan to show in detail; leave blank for all clans",
)
async def dashboard_show(interaction: discord.Interaction, clan: str | None = None) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "dashboard_show"):
        return
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    await record_command_event(interaction, "dashboard_show", clan_name=clan)

    if clan is not None:
        clan_record = bot.database.require_clan(guild.id, clan)
        totals = bot.database.get_category_totals(guild.id, clan_record.name)
        embed = build_clan_dashboard_embed(clan_record, totals)
        await interaction.response.send_message(embed=embed)
        return

    clans = bot.database.list_clans(guild.id)
    if not clans:
        await interaction.response.send_message(embed=clean_embed("Dashboard", ["No clans exist yet."]))
        return

    embeds: list[discord.Embed] = []
    current_embed = discord.Embed(title="Dashboard", color=discord.Color.blurple())
    field_count = 0
    for clan_record in clans:
        totals = bot.database.get_category_totals(guild.id, clan_record.name)
        totals_text = " | ".join(f"{name} `{value}`" for name, value in sorted(totals.items())) or "none"
        ratio_text = "n/a"
        if clan_record.cat_count > 0 and totals:
            ratio_text = " | ".join(
                f"{name} `{value / clan_record.cat_count:.2f}`" for name, value in sorted(totals.items())
            )
        value = (
            f"tracking `{clan_record.tracking_mode}`\n"
            f"cats `{clan_record.cat_count}` | alerts `{clan_record.alerts_enabled}`\n"
            f"totals {totals_text}\n"
            f"ratios {ratio_text}"
        )
        if field_count == 25:
            embeds.append(current_embed)
            current_embed = discord.Embed(title="Dashboard", color=discord.Color.blurple())
            field_count = 0
        current_embed.add_field(name=clan_record.name, value=value, inline=False)
        field_count += 1
    if field_count:
        embeds.append(current_embed)

    await interaction.response.send_message(embed=embeds[0])
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed)


@app_commands.command(name="quick_start", description="Step-by-step guide for setting up items and territories from scratch.")
async def quick_start(interaction: discord.Interaction) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    if not await require_access(interaction, bot.database, "quick_start"):
        return
    await record_command_event(interaction, "quick_start")
    lines = [
        "**Step 1 — Create a clan** `(Admin)`",
        "`/clan_create name: ThunderClan tracking_mode: manual cat_count: 20`",
        "",
        "**Step 2 — Add categories and items** `(Admin)`",
        "`/category_create name: prey`",
        "`/item_add category: prey name: mouse`",
        "`/category_create name: herb`",
        "`/item_add category: herb name: marigold`",
        "Then link items to clans with `/clan_item_link` or to specific territories with `/territory_item_set`.",
        "",
        "**Step 3 — Create a territory** `(Mod)`",
        "`/territory_create name: pineforest clan: ThunderClan`",
        "Then use territory-item commands to link weighted items explicitly.",
        "",
        "**Step 4 — Link items to the territory** `(Mod)`",
        "`/territory_item_set territory: pineforest category: prey item_name: mouse weight: 5`",
        "Higher `weight` means a higher drop chance relative to other items. Add `seasonal_modifier:` to scale the weight up or down with seasons (default `1.0` = no change).",
        "",
        "**Step 5 — Verify your setup**",
        "`/weight_breakdown clan: ThunderClan` — lists every linked item with its 0.0–1.0 drop probability.",
        "`/territory_show territory: pineforest` — shows all weighted items for that territory.",
        "",
        "**Step 6 — Roll for any category** `(User)`",
        "`/roll_forage category: prey territory: pineforest`",
        "Results are added to the clan's storage automatically.",
        "",
        "**Optional — Seasonal modifiers**",
        "`/territory_item_seasonal_set` — per-item seasonal scale for a territory.",
        "`/seasonal_modifier_set scope: clan clan: ThunderClan value: 0.8` — apply a multiplier to every item in a clan's territories.",
        "`/seasonal_modifier_set scope: global value: 0.5` — apply a server-wide multiplier on top of all other modifiers.",
        "All four multipliers stack: `weight × item_seasonal × clan_seasonal × global_seasonal`.",
        "",
        "**Item consumption**",
        "`/use_item category: herb item_name: marigold clan: thunderclan amount: 1`",
        "",
        "**Need more?** Use `/preyherb_help` to see every command for your access tier.",
    ]
    embeds = chunk_lines_for_embed("Quick Start Guide", lines)
    await interaction.response.send_message(embed=embeds[0], ephemeral=True)
    for embed in embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=True)


@app_commands.command(name="preyherb_help", description="Show commands available for an access tier with usage tips.")
@app_commands.describe(
    access_level="Access tier to show help for; leave blank to use your own tier",
    category="Optional category filter (test, permissions, storage, etc.)",
)
async def preyherb_help(
    interaction: discord.Interaction,
    access_level: str | None = None,
    category: Literal[
        "test",
        "clan-tracking",
        "items-territories",
        "storage-rolling",
        "validation-imports",
        "audit-dashboard",
        "permissions-access",
        "help",
    ]
    | None = None,
) -> None:
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(embed=clean_embed("Server Only", ["This command only works in a server."]), ephemeral=True)
        return

    base_level = get_base_member_access_level(interaction, bot.database)
    effective_level = get_member_access_level(interaction, bot.database)
    levels = bot.database.get_access_level_ranks(guild.id)
    if access_level is not None:
        target_level = access_level.strip().lower()
        if target_level not in levels:
            await interaction.response.send_message(
                embed=clean_embed("Unknown Access Level", [f"`{target_level}` is not defined in this server."]),
                ephemeral=True,
            )
            return
    else:
        target_level = effective_level

    command_guides: dict[str, str] = {
        "test_seed_defaults": "Seed built-in starter categories and items for testing.",
        "test_seed_demo": "Seed defaults and create a demo clan in one step.",
        "clan_create": "Create a clan with tracking mode, cat count, and optional tracking link.",
        "clan_list": "List all clans with tracking mode and cat count.",
        "clan_config": "Update a clan's tracking mode, cat count, alert channel, and optionally rename it.",
        "clan_delete": "Permanently delete a clan and cascade-remove all its storage, members, territory links, and history.",
        "clan_member_add": "Assign a user to a clan for lower-tier storage write access.",
        "clan_member_remove": "Remove a user from a clan assignment.",
        "clan_member_show": "List all users assigned to a clan.",
        "category_create": "Create a new item category (e.g. prey, herb, fish).",
        "category_list": "List categories; optionally show alert thresholds for a clan.",
        "category_threshold_set": "Set an alert threshold — static (fixed count) or dynamic (per-cat ratio).",
        "category_territory_rule_set": "Set territory-link or stat-input requirements globally, per category, or per item.",
        "category_remove_request": "Request category deletion; must be confirmed within 15 minutes.",
        "category_remove_confirm": "Confirm a pending category deletion and cascade-delete all linked data.",
        "item_add": "Create or update an item and optionally set stat requirement fields.",
        "item_edit": "Rename an item, change enabled state, or update stat requirement fields.",
        "clan_item_link": "Link or unlink an item for a clan so all clan territories get that link.",
        "item_remove": "Permanently remove an item from a category.",
        "item_list": "List all configured items, optionally filtered by category.",
        "territory_create": "Create a territory with optional clan ownership.",
        "territory_item_set": "Set an item's drop weight for a territory.",
        "territory_item_set_bulk": "Set one item's drop weight across multiple territories at once.",
        "territory_item_seasonal_set": "Update the seasonal modifier for an item–territory link.",
        "territory_item_remove": "Remove an item's drop link from a territory.",
        "seasonal_modifier_set": "Set the seasonal multiplier for a clan or server-wide.",
        "weight_breakdown": "Show effective drop probabilities with the full weight formula breakdown.",
        "territory_link_set": "Link a territory to a channel for auto-detection during rolls.",
        "territory_link_show": "Show which channels are linked to which territories.",
        "territory_show": "Show weighted items and channel link for a territory.",
        "storage_add": "Add or subtract from an item's quantity in clan storage (positive adds require roll link).",
        "storage_set": "Set an item's exact quantity in clan storage.",
        "storage_show": "Show a clan's stored item quantities with category totals.",
        "roll": "Roll a plain d20 or d100 with optional mod, buff, and debuff.",
        "roll_forage": "Roll a d20, evaluate territory/stat requirements, and optionally store the result.",
        "test_roll_forage": "Simulate a forage roll (including stat gating) without storing anything.",
        "roll_config_show": "Show the current roll total bands and their find counts.",
        "roll_config_set": "Replace roll total bands with custom min–max:dose entries.",
        "roll_config_reset": "Reset roll total bands to built-in defaults.",
        "preview_thread_link": "Resolve a Discord thread and show starter content plus parser output used by imports/tracking.",
        "preview_forum_count": "Resolve a forum link/ID and show active, archived, and total thread counts the bot can access.",
        "territory_link_validate": "Check territory and category linkage coverage for gaps.",
        "system_check": "Run a broad health check across clans, territories, and linkages.",
        "linkage_show": "Show item–territory linkages grouped by category, territory, or clan/region.",
        "import_csv_examples": "Show example CSV formats for the import commands.",
        "import": "Unified import from csv/forum/thread; set confirm:False to preview without saving.",
        "preview_message_link": "Fetch a Discord message by link and show the raw content, embed preview, and parser matches.",
        "alert_config": "Configure low-storage alerts (channel and enabled flag).",
        "use_item": "Deduct an item from clan storage and trigger alert checks.",
        "char_stat_set": "Set character stat values used for item requirement checks.",
        "char_stat_show": "Show stored character stat values.",
        "audit_log_show": "View recent storage changes and commands for a clan (retention: 31 days).",
        "audit_undo_last": "Roll back the most recent eligible storage change for a clan.",
        "audit_export_json": "Download audit entries as JSON for a selected timeframe (up to 31 days).",
        "audit_clear": "Delete audit entries after explicit confirmation; supports clan/category/territory filters.",
        "dashboard_show": "Show a storage dashboard for one clan or all clans.",
        "impersonate_access": "Temporarily view the bot as a lower access tier.",
        "permission_set": "Map a Discord role or user to an access tier.",
        "permission_show": "Show all configured role and user access mappings.",
        "access_level_create": "Create or update a custom access tier with a numeric rank.",
        "access_level_remove": "Remove a custom access tier and clean up its mappings.",
        "access_level_list": "List built-in and custom access tiers with ranks.",
        "command_access_set": "Override the required access tier for a command.",
        "command_access_reset": "Clear a command access override and restore the default.",
        "command_access_show": "Show effective required access levels for all commands.",
        "config_show": "Show clan config details or the full command access overview.",
        "preyherb_help": "Show commands available for an access tier with usage and guide.",
        "quick_start": "Step-by-step guide for setting up categories, items, and territories.",
    }

    command_usage: dict[str, str] = {
        "test_seed_defaults": "/test_seed_defaults",
        "test_seed_demo": "/test_seed_demo clan_name: tracking_mode: cat_count:",
        "clan_create": "/clan_create name: tracking_mode: tracking_link: cat_count:",
        "clan_list": "/clan_list",
        "clan_config": "/clan_config clan: new_name: tracking_mode: tracking_link: cat_count: alerts_enabled: alert_channel: roll_log_channel:",
        "clan_delete": "/clan_delete clan: confirm:",
        "clan_member_add": "/clan_member_add clan: member:",
        "clan_member_remove": "/clan_member_remove clan: member:",
        "clan_member_show": "/clan_member_show clan:",
        "category_create": "/category_create name:",
        "category_list": "/category_list clan:",
        "category_threshold_set": "/category_threshold_set clan: category: threshold: mode:",
        "category_territory_rule_set": "/category_territory_rule_set rule: scope: required: category: item_name: force:",
        "category_remove_request": "/category_remove_request category:",
        "category_remove_confirm": "/category_remove_confirm category:",
        "item_add": "/item_add category: name: enabled: required_stat: required_stat_name:",
        "item_edit": "/item_edit category: name: new_name: enabled: required_stat: required_stat_name:",
        "clan_item_link": "/clan_item_link clan: category: item_name: weight: seasonal_modifier: remove:",
        "item_remove": "/item_remove category: name:",
        "item_list": "/item_list category:",
        "territory_create": "/territory_create name: clan: linked_channel:",
        "territory_item_set": "/territory_item_set territory: category: item_name: weight: seasonal_modifier:",
        "territory_item_set_bulk": "/territory_item_set_bulk territories: category: item_name: weight: seasonal_modifier:",
        "territory_item_seasonal_set": "/territory_item_seasonal_set territory: category: item_name: seasonal_modifier:",
        "territory_item_remove": "/territory_item_remove territory: category: item_name:",
        "seasonal_modifier_set": "/seasonal_modifier_set scope: value: clan:",
        "weight_breakdown": "/weight_breakdown clan: category: territory: item_name:",
        "territory_link_set": "/territory_link_set territory: channel_or_link:",
        "territory_link_show": "/territory_link_show territory:",
        "territory_show": "/territory_show territory: category:",
        "storage_add": "/storage_add clan: category: item_name: amount: roll_message_link:",
        "storage_set": "/storage_set clan: category: item_name: amount:",
        "storage_show": "/storage_show clan: category:",
        "roll": "/roll die: mod: buff: debuff: clan:",
        "roll_forage": "/roll_forage category: modifier: clan: territory: stat:",
        "test_roll_forage": "/test_roll_forage category: modifier: clan: territory: stat:",
        "roll_config_show": "/roll_config_show",
        "roll_config_set": "/roll_config_set ranges:",
        "roll_config_reset": "/roll_config_reset",
        "preview_thread_link": "/preview_thread_link link:",
        "preview_forum_count": "/preview_forum_count link:",
        "territory_link_validate": "/territory_link_validate scope:",
        "system_check": "/system_check",
        "linkage_show": "/linkage_show view: name:",
        "import_csv_examples": "/import_csv_examples",
        "import": "/import type: link: category: stats: clan_region: confirm:",
        "preview_message_link": "/preview_message_link link:",
        "alert_config": "/alert_config clan: enabled: alert_channel:",
        "use_item": "/use_item category: item_name: clan: amount:",
        "char_stat_set": "/char_stat_set stat_name: value: member:",
        "char_stat_show": "/char_stat_show member:",
        "audit_log_show": "/audit_log_show clan: limit:",
        "audit_undo_last": "/audit_undo_last clan:",
        "audit_export_json": "/audit_export_json since_days: clan:",
        "audit_clear": "/audit_clear clan: category: territory: confirm:",
        "dashboard_show": "/dashboard_show clan:",
        "impersonate_access": "/impersonate_access access_level:",
        "permission_set": "/permission_set access_level: role: user:",
        "permission_show": "/permission_show",
        "access_level_create": "/access_level_create name: rank:",
        "access_level_remove": "/access_level_remove name:",
        "access_level_list": "/access_level_list",
        "command_access_set": "/command_access_set command_name: access_level:",
        "command_access_reset": "/command_access_reset command_name:",
        "command_access_show": "/command_access_show",
        "config_show": "/config_show clan:",
        "preyherb_help": "/preyherb_help access_level: category:",
        "quick_start": "/quick_start",
    }

    command_order = [
        "test_seed_defaults",
        "test_seed_demo",
        "clan_create",
        "clan_list",
        "clan_config",
        "clan_delete",
        "clan_member_add",
        "clan_member_remove",
        "clan_member_show",
        "category_create",
        "category_list",
        "category_threshold_set",
        "category_territory_rule_set",
        "category_remove_request",
        "category_remove_confirm",
        "item_add",
        "item_edit",
        "clan_item_link",
        "item_remove",
        "item_list",
        "territory_create",
        "territory_item_set",
        "territory_item_set_bulk",
        "territory_item_seasonal_set",
        "territory_item_remove",
        "seasonal_modifier_set",
        "weight_breakdown",
        "territory_link_set",
        "territory_link_show",
        "territory_show",
        "storage_add",
        "storage_set",
        "storage_show",
        "roll",
        "roll_forage",
        "test_roll_forage",
        "roll_config_show",
        "roll_config_set",
        "roll_config_reset",
        "preview_thread_link",
        "preview_forum_count",
        "territory_link_validate",
        "system_check",
        "linkage_show",
        "import_csv_examples",
        "import",
        "preview_message_link",
        "alert_config",
        "use_item",
        "char_stat_set",
        "char_stat_show",
        "audit_log_show",
        "audit_undo_last",
        "audit_export_json",
        "audit_clear",
        "dashboard_show",
        "impersonate_access",
        "permission_set",
        "permission_show",
        "access_level_create",
        "access_level_remove",
        "access_level_list",
        "command_access_set",
        "command_access_reset",
        "command_access_show",
        "config_show",
        "preyherb_help",
        "quick_start",
    ]

    fixed_access: dict[str, str] = {
        "clan_list": "user",
        "item_list": "user",
        "territory_show": "user",
        "weight_breakdown": "user",
        "permission_show": "user",
        "audit_log_show": "user",
        "audit_undo_last": "user",
        "audit_clear": "admin",
        "dashboard_show": "user",
        "impersonate_access": "mod",
        "quick_start": "user",
    }

    command_categories: list[tuple[str, str, list[str]]] = [
        ("test", "Test", ["test_seed_defaults", "test_seed_demo", "test_roll_forage"]),
        (
            "clan-tracking",
            "Clan Or Region And Tracking",
            [
                "clan_create",
                "clan_list",
                "clan_config",
                "clan_delete",
                "clan_member_add",
                "clan_member_remove",
                "clan_member_show",
                "category_create",
                "category_list",
                "category_threshold_set",
                "category_territory_rule_set",
                "category_remove_request",
                "category_remove_confirm",
                "preview_thread_link",
                "preview_forum_count",
            ],
        ),
        (
            "items-territories",
            "Items And Territories",
            [
                "item_add",
                "item_edit",
                "clan_item_link",
                "item_remove",
                "item_list",
                "territory_create",
                "territory_item_set",
                "territory_item_set_bulk",
                "territory_item_seasonal_set",
                "territory_item_remove",
                "seasonal_modifier_set",
                "weight_breakdown",
                "territory_link_set",
                "territory_link_show",
                "territory_show",
                "linkage_show",
            ],
        ),
        (
            "storage-rolling",
            "Storage And Rolling",
            [
                "storage_add",
                "storage_set",
                "storage_show",
                "char_stat_set",
                "char_stat_show",
                "roll",
                "roll_forage",
                "roll_config_show",
                "roll_config_set",
                "roll_config_reset",
                "use_item",
                "alert_config",
            ],
        ),
        (
            "validation-imports",
            "Validation And Imports",
            [
                "territory_link_validate",
                "system_check",
                "import_csv_examples",
                "import",
                "preview_message_link",
            ],
        ),
        (
            "audit-dashboard",
            "Audit And Dashboard",
            ["audit_log_show", "audit_undo_last", "audit_export_json", "audit_clear", "dashboard_show"],
        ),
        (
            "permissions-access",
            "Permissions And Access",
            [
                "permission_set",
                "permission_show",
                "access_level_create",
                "access_level_remove",
                "access_level_list",
                "command_access_set",
                "command_access_reset",
                "command_access_show",
                "impersonate_access",
                "config_show",
            ],
        ),
        ("help", "Help", ["preyherb_help", "quick_start"]),
    ]

    category_embeds: list[discord.Embed] = []
    intro_lines = [f"Commands for access level `{target_level}`."]
    if access_level is None and effective_level != base_level:
        intro_lines.append(
            f"Currently impersonating `{effective_level}`. Base access: `{base_level}`. Use `/impersonate_access` with no access level to clear."
        )

    for category_key, category_title, category_commands in command_categories:
        if category is not None and category_key != category:
            continue
        lines: list[str] = []
        for command_name in category_commands:
            required_level = fixed_access.get(
                command_name,
                bot.database.get_command_access_level(guild.id, command_name, COMMAND_ACCESS),
            )
            if not access_allows(bot.database, guild.id, target_level, required_level):
                continue
            usage = command_usage.get(command_name, f"/{command_name}")
            guide = command_guides.get(command_name, "No guide available.")
            lines.append(f"`{usage}`")
            lines.append(f"requires `{required_level}` · {guide}")
        if not lines:
            continue
        if not category_embeds:
            lines = intro_lines + lines
        category_embeds.extend(chunk_lines_for_embed(f"PreyHerb Help - {category_title}", lines))

    if not category_embeds:
        if category is not None:
            category_embeds = [
                clean_embed(
                    "PreyHerb Help",
                    [f"No commands available for `{target_level}` in `{category}`."],
                )
            ]
        else:
            category_embeds = [clean_embed("PreyHerb Help", [f"No commands available for `{target_level}`."])]

    await interaction.response.send_message(embed=category_embeds[0], ephemeral=True)
    for embed in category_embeds[1:]:
        await interaction.followup.send(embed=embed, ephemeral=True)


@clan_config.autocomplete("clan")
@clan_delete.autocomplete("clan")
@clan_member_add.autocomplete("clan")
@clan_member_remove.autocomplete("clan")
@clan_member_show.autocomplete("clan")
@territory_create.autocomplete("clan")
@storage_add.autocomplete("clan")
@storage_set.autocomplete("clan")
@storage_show.autocomplete("clan")
@roll.autocomplete("clan")
@roll_forage.autocomplete("clan")
@test_roll_forage.autocomplete("clan")
@alert_config.autocomplete("clan")
@audit_log_show.autocomplete("clan")
@audit_undo_last.autocomplete("clan")
@audit_export_json.autocomplete("clan")
@audit_clear.autocomplete("clan")
@dashboard_show.autocomplete("clan")
@config_show.autocomplete("clan")
@use_item.autocomplete("clan")
@clan_item_link.autocomplete("clan")
@import_items.autocomplete("clan_region")
@seasonal_modifier_set.autocomplete("clan")
@weight_breakdown.autocomplete("clan")
@category_list.autocomplete("clan")
@category_threshold_set.autocomplete("clan")
async def clan_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await clan_name_autocomplete(interaction, current)


@item_add.autocomplete("category")
@item_edit.autocomplete("category")
@item_remove.autocomplete("category")
@item_list.autocomplete("category")
@territory_item_set.autocomplete("category")
@territory_item_set_bulk.autocomplete("category")
@territory_item_seasonal_set.autocomplete("category")
@territory_item_remove.autocomplete("category")
@weight_breakdown.autocomplete("category")
@territory_show.autocomplete("category")
@storage_add.autocomplete("category")
@storage_set.autocomplete("category")
@storage_show.autocomplete("category")
@roll_forage.autocomplete("category")
@test_roll_forage.autocomplete("category")
@use_item.autocomplete("category")
@clan_item_link.autocomplete("category")
@import_items.autocomplete("category")
@territory_link_validate.autocomplete("category")
@category_threshold_set.autocomplete("category")
@category_territory_rule_set.autocomplete("category")
@category_remove_request.autocomplete("category")
@category_remove_confirm.autocomplete("category")
@audit_clear.autocomplete("category")
async def category_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await category_name_autocomplete(interaction, current)


@territory_item_set.autocomplete("territory")
@territory_item_seasonal_set.autocomplete("territory")
@territory_item_remove.autocomplete("territory")
@territory_link_set.autocomplete("territory")
@territory_link_show.autocomplete("territory")
@territory_show.autocomplete("territory")
@roll_forage.autocomplete("territory")
@test_roll_forage.autocomplete("territory")
@audit_clear.autocomplete("territory")
async def territory_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await territory_name_autocomplete(interaction, current)


@item_add.autocomplete("name")
@item_edit.autocomplete("name")
@item_remove.autocomplete("name")
async def item_remove_name_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await item_name_autocomplete(interaction, current)


@territory_item_set.autocomplete("item_name")
@territory_item_seasonal_set.autocomplete("item_name")
@territory_item_remove.autocomplete("item_name")
@weight_breakdown.autocomplete("item_name")
@storage_add.autocomplete("item_name")
@storage_set.autocomplete("item_name")
@use_item.autocomplete("item_name")
@clan_item_link.autocomplete("item_name")
@category_territory_rule_set.autocomplete("item_name")
async def item_name_ac(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await item_name_autocomplete(interaction, current)


@command_access_set.autocomplete("command_name")
@command_access_reset.autocomplete("command_name")
async def command_name_ac(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    return await command_name_autocomplete(interaction, current)


@permission_set.autocomplete("access_level")
@command_access_set.autocomplete("access_level")
@impersonate_access.autocomplete("access_level")
@preyherb_help.autocomplete("access_level")
async def access_level_ac(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    current_lower = current.strip().lower()
    levels = [name for name, _, _ in bot.database.list_access_levels(interaction.guild.id)]
    matches = [name for name in levels if current_lower in name] if current_lower else levels
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


@linkage_show.autocomplete("view")
async def linkage_view_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    current_lower = current.strip().lower()
    choices = ["territory", "clan", *bot.database.list_categories(interaction.guild.id)]
    matches = [name for name in choices if current_lower in name] if current_lower else choices
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


@weight_breakdown.autocomplete("territory")
async def weight_breakdown_territory_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    bot = interaction.client
    assert isinstance(bot, PreyHerbTrackerBot)
    clan_name: str | None = getattr(interaction.namespace, "clan", None)
    current_lower = current.strip().lower()
    if clan_name:
        try:
            names = bot.database.list_territory_names_by_clan(interaction.guild.id, clan_name)
        except ValueError:
            names = []
    else:
        names = bot.database.list_territory_names(interaction.guild.id)
    matches = [n for n in names if current_lower in n] if current_lower else names
    return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


def build_bot(settings):
    bot = PreyHerbTrackerBot(settings)
    for command in (
        test_seed_defaults,
        test_seed_demo,
        clan_create,
        clan_list,
        clan_config,
        clan_delete,
        clan_member_add,
        clan_member_remove,
        clan_member_show,
        category_create,
        category_list,
        category_threshold_set,
        category_territory_rule_set,
        category_remove_request,
        category_remove_confirm,
        item_add,
        item_edit,
        clan_item_link,
        item_remove,
        item_list,
        territory_create,
        territory_item_set,
        territory_item_set_bulk,
        territory_item_seasonal_set,
        territory_item_remove,
        seasonal_modifier_set,
        weight_breakdown,
        territory_link_set,
        territory_link_show,
        territory_show,
        storage_add,
        storage_set,
        storage_show,
        roll,
        roll_forage,
        test_roll_forage,
        roll_config_show,
        roll_config_set,
        roll_config_reset,
        preview_thread_link,
        preview_forum_count,
        territory_link_validate,
        system_check,
        linkage_show,
        import_csv_examples,
        import_items,
        preview_message_link,
        alert_config,
        use_item,
        char_stat_set,
        char_stat_show,
        audit_log_show,
        audit_undo_last,
        audit_export_json,
        audit_clear,
        dashboard_show,
        impersonate_access,
        permission_set,
        permission_show,
        access_level_create,
        access_level_remove,
        access_level_list,
        command_access_set,
        command_access_reset,
        command_access_show,
        config_show,
        preyherb_help,
        quick_start,
    ):
        bot.tree.add_command(command)
    return bot




