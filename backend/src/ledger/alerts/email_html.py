"""Email-safe HTML building blocks (table layout, inline styles) shared by alerts, the digest and questions."""

import html

from ledger.alerts.mailer import LOGO_CID

FONT = "'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
NAVY = "#1d2d3d"
TEXT = "#1d1f20"
MUTED = "#5f6b76"
LINK = "#416180"
DIVIDER = "#e3e5e8"
PAGE_BG = "#f2f2f3"
# (foreground, background) per notification tone; matches the app's --color-over / --color-pace / accent.
TONES = {
    "over": ("#a8433c", "#f8e6e4"),
    "pace": ("#96690f", "#f7ecd3"),
    "info": ("#416180", "#eef6ff"),
    "good": ("#3d6b4f", "#e5f1e9"),
}


def esc(s: str) -> str:
    return html.escape(s)


def multiline(s: str) -> str:
    return esc(s).replace("\n", "<br>")


def link(text: str, url: str | None, color: str = LINK) -> str:
    if not url:
        return esc(text)
    return f'<a href="{esc(url)}" style="color:{color};text-decoration:none">{esc(text)}</a>'


def badge(label: str, tone: str = "info") -> str:
    fg, bg = TONES[tone]
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:3px;background:{bg};color:{fg};'
        f'font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase">{esc(label)}</span>'
    )


def button(label: str, url: str | None) -> str:
    if not url:
        return ""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:20px 0 4px"><tr>'
        f'<td style="background:{NAVY};border-radius:4px">'
        f'<a href="{esc(url)}" style="display:inline-block;padding:10px 20px;font-family:{FONT};font-size:14px;'
        f'font-weight:600;color:#ffffff;text-decoration:none">{esc(label)}</a></td></tr></table>'
    )


def card(inner: str, tone: str = "info") -> str:
    """A notification block with a coloured left rule."""
    fg, _ = TONES[tone]
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 12px;'
        f'border:1px solid {DIVIDER};border-left:4px solid {fg};border-radius:4px"><tr>'
        f'<td style="padding:12px 16px;font-family:{FONT};font-size:14px;color:{TEXT}">{inner}</td></tr></table>'
    )


def alert_card(label: str, tone: str, title: str, body: str, url: str | None) -> str:
    view = f'<div style="margin-top:8px;font-size:13px">{link("View in Ledger →", url)}</div>' if url else ""
    return card(
        f"{badge(label, tone)}"
        f'<div style="margin-top:6px;font-size:16px;font-weight:600">{link(title, url, TEXT)}</div>'
        f'<div style="margin-top:4px;color:{MUTED};line-height:1.45">{multiline(body)}</div>{view}',
        tone,
    )


def quote(text: str) -> str:
    fg, bg = TONES["info"]
    return (
        f'<div style="margin:0 0 20px;padding:14px 16px;background:{bg};border-left:4px solid {fg};'
        f'font-size:15px;line-height:1.5;color:{TEXT}">{multiline(text)}</div>'
    )


def stats(items: list[tuple[str, str, str | None]]) -> str:
    """A row of KPI cells: (label, value, note)."""
    cells = "".join(
        f'<td style="padding:12px 14px;border:1px solid {DIVIDER};vertical-align:top;width:{100 // len(items)}%">'
        f'<div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:{MUTED}">{esc(label)}</div>'
        f'<div style="margin-top:4px;font-size:22px;font-weight:600;color:{TEXT}">{esc(value)}</div>'
        + (f'<div style="margin-top:2px;font-size:12px;color:{MUTED}">{esc(note)}</div>' if note else "")
        + "</td>"
        for label, value, note in items
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border-collapse:collapse;margin:0 0 8px;font-family:{FONT}"><tr>{cells}</tr></table>'
    )


def section(heading: str, lines: list[tuple[str, str | None]]) -> str:
    rows = "".join(
        f'<tr><td style="padding:7px 0;border-bottom:1px solid {DIVIDER};font-size:14px;color:{TEXT}">'
        f"{link(text, url)}</td></tr>"
        for text, url in lines
    )
    return (
        f'<h3 style="margin:22px 0 4px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:{MUTED};'
        f'font-weight:600">{esc(heading)}</h3>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-family:{FONT}">{rows}</table>'
    )


def layout(*, kicker: str, heading: str, body: str, preheader: str = "", footer: str = "") -> str:
    # Padding after the preheader keeps the start of the body from trailing into the inbox preview.
    pad = "&#847;&zwnj;&nbsp;" * 60
    hidden = (
        '<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all">'
        f"{esc(preheader)}{pad}</div>"
        if preheader
        else ""
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">'
        f"<title>{esc(heading)}</title></head>"
        f'<body style="margin:0;padding:0;background:{PAGE_BG}">{hidden}'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE_BG}">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px">'
        # Header bar with the logo and wordmark.
        f'<tr><td style="background:{NAVY};padding:14px 24px;border-radius:6px 6px 0 0">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="vertical-align:middle;width:44px"><img src="cid:{LOGO_CID}" width="36" height="36" alt="Ledger" '
        'style="display:block;border:0;border-radius:7px"></td>'
        f'<td style="vertical-align:middle;font-family:{FONT};font-size:20px;font-weight:600;color:#ffffff;'
        'letter-spacing:-.01em">Ledger</td>'
        f'<td align="right" style="vertical-align:middle;font-family:{FONT};font-size:11px;letter-spacing:.1em;'
        f'text-transform:uppercase;color:#94bce3">{esc(kicker)}</td>'
        "</tr></table></td></tr>"
        # Body.
        f'<tr><td style="background:#ffffff;padding:24px;font-family:{FONT};font-size:14px;line-height:1.45;'
        f'color:{TEXT};border:1px solid {DIVIDER};border-top:0">'
        f'<h1 style="margin:0 0 16px;font-size:22px;line-height:1.25;font-weight:600;color:{TEXT}">{esc(heading)}</h1>'
        f"{body}</td></tr>"
        # Footer.
        f'<tr><td style="padding:14px 24px;font-family:{FONT};font-size:12px;line-height:1.5;color:{MUTED}">'
        f"{footer}</td></tr>"
        "</table></td></tr></table></body></html>"
    )
