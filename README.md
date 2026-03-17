# PreyHerbTracker

PreyHerbTracker is a Discord slash-command bot for clan inventory management with role-based access, dynamic item categories, territory-linked roll logic, audit tooling, and CSV/XLSX-assisted configuration workflows.

## What It Supports

- Clan management with off/manual/forum/spreadsheet cat tracking modes
- Built-in role tiers: admin, mod, user
- Custom access tiers with admin-defined rank values
- Per-command access overrides
- Access impersonation for admin/mod testing
- Clan membership enforcement for lower-tier storage actions
- Dynamic item categories per server (not limited to prey/herb)
- Territory-based weighted roll pools with optional seasonal modifier
- Territory channel/thread linking for auto-detection during rolls
- Territory-link and stat-input requirement rules at global/category/item scope
- Roll configuration per guild (custom total bands to find counts)
- Storage history and command audit logs
- Audit JSON export with a 31-day retention window
- Undo latest eligible storage action (audit_undo_last)
- Dashboard views and system/linkage validation commands
- Catalog import/preview from CSV text or URL sources (CSV/XLSX supported for parsing rows)
- Forum-thread catalog import with optional territory-link attempts
- Tracking sync from forum/spreadsheet links for cat counts
- Embed-first response UI and categorized help output

## Project Layout

- bot.py - startup entry point
- setup_database.py - schema/bootstrap setup
- src/preyherbtracker/bot_app.py - slash commands and interaction flow
- src/preyherbtracker/database.py - SQLite persistence and migrations
- src/preyherbtracker/rolling.py - forage roll engine
- src/preyherbtracker/tracking.py - forum/CSV/XLSX parsing helpers
- tests/ - unit tests
- data/examples/ - sample CSV import files

## Setup

1. Create and invite a Discord bot with applications.commands scope.
2. Configure environment values (DISCORD_TOKEN, optional BOT_GUILD_ID, optional DATABASE_PATH).
3. Install dependencies and run the bot.

```powershell
Set-Location "f:\VSCode\PreyHerbTracker"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
python setup_database.py
python bot.py
```

## First-Run Flow

1. Seed starter data:
   - /test_seed_demo clan_name:birchclan tracking_mode:manual cat_count:12
2. Create categories (optional if you only use defaults):
   - /category_create name:prey
   - /category_create name:herb
   - /category_create name:fish
3. Create territory and links:
   - /territory_create name:pineforest linked_channel:<id-or-link>
   - /territory_item_set territory:pineforest category:prey item_name:mouse weight:4 seasonal_modifier:1.25
4. Try rolls:
   - /test_roll_forage category:prey modifier:3 clan:birchclan stat:12
   - /roll_forage category:prey modifier:3 clan:birchclan stat:12
5. Inspect storage and health:
   - /storage_show clan:birchclan
   - /storage_add clan:birchclan category:prey item_name:mouse amount:1 roll_message_link:<discord-message-link>
   - /audit_export_json since_days:7 clan:birchclan
   - /system_check

## Category Management

- /category_create name:
- /category_list clan:
- /category_threshold_set clan: category: threshold: mode:
- /category_territory_rule_set rule: scope: required: category: item_name: force:
- /category_remove_request category:
- /category_remove_confirm category:

Category removal is destructive and requires the two-step request/confirm flow.

### Alert threshold modes

The `mode` parameter on `/category_threshold_set` controls how the threshold is evaluated:

| Mode | Trigger condition | Example value |
|---|---|---|
| `dynamic` (default) | `total / cat_count < threshold` | `0.5` = half an item per cat |
| `static` | `total < threshold` | `20` = fewer than 20 items total |

### Territory And Stat Rules

Use /category_territory_rule_set to control rule behavior by scope:

- rule `territory_link`: whether items must be linked to a territory before they can roll
- rule `stat_input`: whether roll commands must include `stat:` for matching items

- scope `global`: default rule for all categories/items
- scope `category`: overrides the global rule for one category
- scope `item`: overrides category/global for one specific item

Behavior:
- for `territory_link`: required `true` means item must be linked to a territory; required `false` allows global fallback rolling
- for `stat_input`: required `true` means a stat value must be supplied on forage rolls for matching items

Force propagation:
- `scope:global force:true` pushes the setting to all category and item overrides
- `scope:category force:true` pushes the setting to all item overrides in that category
- with `force:false`, existing lower-level item overrides are preserved

## Item Consumption

/use_item category: item_name: clan: amount:

## Imports

Use /import_catalog_preview before /import_catalog_csv.

Forum import command:
- /import_forum_catalog forum_post_link: category: try_link_territories: weight: clan_fallback:

Targets:
- territories
- items
- links

Items CSV columns:
- category,name,dosage_modifier,enabled

Links CSV columns:
- territory,category,item_name,weight,seasonal_modifier

## Help And Categories

Use /preyherb_help to show commands for your current access tier.

Filters:
- test
- clan-tracking
- items-territories
- storage-rolling
- validation-imports
- audit-dashboard
- permissions-access
- help

## Access Levels

- /access_level_create name: rank:
- /access_level_remove name:
- /access_level_list

Admins can create custom access tiers (for example: `healer`, `quartermaster`) and then use:

- /permission_set access_level: role:
- /permission_set access_level: user:
- /command_access_set command_name: access_level:

## Notes

- test_seed_defaults and test_seed_demo seed default starter categories and items.
- audit entries are retained for 31 days.
