from importlib.metadata import PackageNotFoundError, version

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


def _money_format(value) -> str:
    """Format a number with commas and 2 decimal places (e.g. 10,000.00)."""
    try:
        return f"{float(value):,.2f}"
    except ValueError, TypeError:
        return "0.00"


try:
    _app_version = version("glow-worm")
except PackageNotFoundError:
    _app_version = "dev"

templates.env.filters["money"] = _money_format
templates.env.globals["app_version"] = _app_version
