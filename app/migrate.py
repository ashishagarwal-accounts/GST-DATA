"""Run Alembic migrations from code (app startup, setup script, smoke test)."""
from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent


def _config(url: str | None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.cmd_opts = Namespace(x=[f"url={url}"] if url else [])
    cfg.attributes["configure_logger"] = False  # keep uvicorn's logging intact
    return cfg


def upgrade(url: str | None = None) -> None:
    """Bring the database (default: DATABASE_URL) up to the latest schema."""
    command.upgrade(_config(url), "head")


def stamp(url: str) -> None:
    """Mark a database whose tables were created directly (create_all) as up to date."""
    command.stamp(_config(url), "head")
