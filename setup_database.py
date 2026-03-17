from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from preyherbtracker.config import load_settings
from preyherbtracker.database import Database
from preyherbtracker.models import DEFAULT_HERBS, DEFAULT_PREY, ItemCategory


if __name__ == "__main__":
    settings = load_settings()
    database = Database(settings.database_path)
    database.initialize()

    demo_guild_id = 0
    for item_name, dosage in DEFAULT_HERBS:
        database.add_item(demo_guild_id, item_name, ItemCategory.HERB.value, dosage_modifier=dosage, is_default=True)
    for item_name, dosage in DEFAULT_PREY:
        database.add_item(demo_guild_id, item_name, ItemCategory.PREY.value, dosage_modifier=dosage, is_default=True)

    print(f"Database initialized at {settings.database_path}")
    print("Seeded default items for guild id 0. Use /seed_defaults inside Discord for a real server.")
