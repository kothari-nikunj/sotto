from __future__ import annotations


_FAVICON = ("<link rel='icon' href=\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
            "viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23221c12'/%3E%3Ctext "
            "x='16' y='23' font-family='Georgia,serif' font-style='italic' font-size='20' "
            "font-weight='600' fill='%2352b087' text-anchor='middle'%3ES%3C/text%3E%3C/svg%3E\">")


def _page_head(title: str, head_extra: str = "", body_class: str = "") -> str:
    cls = f" class='{body_class}'" if body_class else ""
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='color-scheme' content='light dark'>"
            f"{_FAVICON}"
            "<link rel='stylesheet' href='/static/app.css'>"
            "<link rel='stylesheet' href='/static/setup.css'>"
            f"{head_extra}<title>{title}</title></head><body{cls}>")


def _nav() -> str:
    """The shared site navigation — same destinations as the /app dashboard's own nav, with
    Integrations (this surface) marked current. Rendered on /setup; the transient satellite pages
    (Google auth, connector results) use the narrow shell instead."""
    return ("<header class='sidebar'><a class='wordmark' href='/app'>Sotto</a>"
            "<nav class='nav'>"
            "<a href='/app#today'>Today</a>"
            "<a href='/app#cadence'>Cadence</a>"
            "<a href='/app#loops'>Loops</a>"
            "<a href='/app#briefs'>Briefs</a>"
            "<a href='/app#people'>People</a>"
            "<a href='/app#learned'>Learned</a>"
            "<a href='/app#record'>Record</a>"
            "<a href='/setup' class='active' aria-current='page'>Integrations</a>"
            "</nav></header>")


def _narrow_page(title: str, inner: str, head_extra: str = "", back_href: str = "/setup") -> str:
    """Minimal shared shell for the transient setup satellites (Google auth/exchange,
    connector success/error): same fonts/palette via the two stylesheets, no sidebar, one quiet way
    back to the wizard."""
    return (_page_head(title, head_extra=head_extra)
            + "<div class='site'><main class='content narrow'>"
            + inner
            + f"<p><a class='btn-quiet' href='{back_href}'>← Integrations</a></p>"
            "</main></div></body></html>")


def _plain_tile(heading: str, body_html: str) -> str:
    return (f"<section class='tile'><div class='tile-head'><h2 class='tile-title'>{heading}</h2></div>"
            f"<div class='tile-body'>{body_html}</div></section>")


def _connect_page(heading: str, body_html: str) -> str:
    return _narrow_page("Sotto — connect a service", _plain_tile(heading, body_html))


def _connect_error_page(step: str, detail: str) -> str:
    """Every connector failure page NAMES the failing step (discovery / registration (DCR) / state /
    exchange / …) and quotes the truncated upstream error — the first click IS the validation, so
    the page has to say exactly which leg of the flow broke."""
    import html as _html
    return _connect_page(
        "Connection failed",
        f"<p>Step that failed: <b>{_html.escape(step)}</b>"
        f"{' (DCR)' if step == 'registration' else ''}</p>"
        f"<p class='tile-hint'>{_html.escape(detail)}</p>")


def _humanize_ago(secs: float) -> str:
    m = int(secs // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    if m < 24 * 60:
        return f"{m // 60} h ago"
    return f"{m // (24 * 60)} d ago"


def _tile(num: int, title: str, state: str, body: str) -> str:
    """One wizard tile in the shared markup contract (styled by /static/setup.css): data-state is
    done|todo|optional; the visible state label spells 'to do' out."""
    label = {"done": "done", "todo": "to do", "optional": "optional"}[state]
    return (f"<section class='tile' data-state='{state}'>"
            f"<div class='tile-head'><span class='tile-num'>{num}</span>"
            f"<h2 class='tile-title'>{title}</h2>"
            f"<span class='tile-state'>{label}</span></div>"
            f"<div class='tile-body'>{body}</div></section>")
