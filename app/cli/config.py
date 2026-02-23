import tomllib
from pathlib import Path

import typer

CONFIG_DIR = Path.home() / ".config" / "glow-worm"
CONFIG_FILE = CONFIG_DIR / "config.toml"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        for key, value in config.items():
            f.write(f'{key} = "{value}"\n')


def require_config() -> dict:
    config = load_config()
    missing = [k for k in ("url", "api_key") if not config.get(k)]
    if missing:
        typer.echo(
            f"Missing config: {', '.join(missing)}. "
            "Run `glow config set-url <url>` and `glow config set-key <key>`.",
            err=True,
        )
        raise typer.Exit(1)
    return config
