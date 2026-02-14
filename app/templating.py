from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")


def _money_format(value) -> str:
    """Format a number with commas and 2 decimal places (e.g. 10,000.00)."""
    try:
        return "{:,.2f}".format(float(value))
    except (ValueError, TypeError):
        return "0.00"


templates.env.filters["money"] = _money_format
