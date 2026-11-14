"""Off-canvas menu markup. Every page that extends base.html, and not login."""

from pathlib import Path


def _sidebar_inner(html: str) -> str:
    start = html.index('id="gw-sidebar"')
    end = html.index("</nav>", start)
    return html[start:end]


def _narrow_css() -> str:
    css = Path("app/static/styles.css").read_text()
    start = css.index("@media (max-width: 800px)")
    rest = css[start:]
    depth = 0
    for index, char in enumerate(rest):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return rest[: index + 1]
    raise AssertionError("unclosed @media (max-width: 800px)")


class TestOffCanvasNav:
    def test_pages_with_a_sidebar_include_the_menu(self, authed_client):
        for path in ("/", "/bills", "/budgets"):
            response = authed_client.get(path)
            assert response.status_code == 200, path
            html = response.text
            assert 'id="gw-sidebar"' in html
            assert 'id="gw-menu-btn"' in html
            assert 'id="gw-nav-backdrop"' in html
            assert 'id="gw-collapse-btn"' in html
            assert 'class="gw-signout-btn"' in html
            assert 'hx-post="/logout"' in html
            button = html.split('id="gw-menu-btn"', 1)[0]
            assert 'type="button"' in button[button.rfind("<button") :]
            assert 'aria-controls="gw-sidebar"' in html
            sidebar = _sidebar_inner(html)
            assert "gw-menu-btn" not in sidebar
            assert "gw-nav-backdrop" not in sidebar
            assert html.index("</nav>") < html.index('id="gw-menu-btn"')

    def test_login_has_no_menu(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        assert "gw-menu-btn" not in response.text
        assert "gw-nav-backdrop" not in response.text
        assert "gw-sidebar" not in response.text

    def test_narrow_css_restores_sign_out_on_a_collapsed_rail(self):
        css = Path("app/static/styles.css").read_text()
        block = _narrow_css()
        assert block.index("@media (max-width: 800px)") == 0
        assert ".gw-sidebar.collapsed" in block
        assert "width: var(--sidebar-width)" in block
        assert "--sidebar-collapsed-width" not in block
        sign_out = block.split(".gw-sidebar.collapsed .gw-signout-btn", 1)[1][:160]
        assert "display: flex" in sign_out
        assert "display: none" not in sign_out
        user = block.split(".gw-sidebar.collapsed .gw-user-info", 1)[1][:160]
        assert "display: block" in user
        assert "display: none" not in user
        assert "padding-top: 56px" in block
        assert ".gw-sidebar.is-open ~ #gw-menu-btn" in block
        # The desktop rail still hides Sign out. The override comes later.
        hide_at = css.index(".gw-sidebar.collapsed .gw-signout-btn {")
        assert "display: none" in css[hide_at : hide_at + 80]
        assert hide_at < css.index("@media (max-width: 800px)")
