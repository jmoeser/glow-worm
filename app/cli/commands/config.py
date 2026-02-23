from typing import Annotated

import typer

from app.cli.client import print_json
from app.cli.config import CONFIG_FILE, load_config, save_config

app = typer.Typer(help="Manage CLI configuration.")


@app.command("set-url")
def set_url(
    url: Annotated[str, typer.Argument(help="Server URL, e.g. http://localhost:8000")],
) -> None:
    """Set the glow-worm server URL."""
    config = load_config()
    config["url"] = url.rstrip("/")
    save_config(config)
    typer.echo(f"URL set to: {config['url']}")


@app.command("set-key")
def set_key(
    key: Annotated[str, typer.Argument(help="API key")],
) -> None:
    """Set the API key for authentication."""
    config = load_config()
    config["api_key"] = key
    save_config(config)
    typer.echo("API key saved.")


@app.command("show")
def show(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show current configuration."""
    config = load_config()
    if json_output:
        print_json({**config, "config_file": str(CONFIG_FILE)})
        return
    url = config.get("url", "(not set)")
    raw_key = config.get("api_key", "")
    if not raw_key:
        masked = "(not set)"
    elif len(raw_key) <= 6:
        masked = "***"
    else:
        masked = f"{raw_key[:6]}..."
    typer.echo(f"Server URL: {url}")
    typer.echo(f"API key:    {masked}")
    typer.echo(f"Config:     {CONFIG_FILE}")
