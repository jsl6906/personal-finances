import base64
import re
from email.message import EmailMessage
from functools import cache
from pathlib import Path

import aiosmtplib

from ledger.config import get_settings

EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")
LOGO_CID = "ledger-logo"
LOGO_PATH = Path(__file__).parent / "assets" / "logo.png"


@cache
def logo_png() -> bytes:
    return LOGO_PATH.read_bytes()


def preview_html(html: str) -> str:
    """For in-app previews, where there is no MIME part to reference: the logo as a data URI."""
    return html.replace(f"cid:{LOGO_CID}", "data:image/png;base64," + base64.b64encode(logo_png()).decode())


class MailNotConfigured(RuntimeError):
    pass


def smtp_configured() -> bool:
    s = get_settings()
    return bool(s.smtp_host and (s.smtp_from or s.smtp_username))


def _one_line(s: str) -> str:
    return " ".join(s.split())[:200]


async def send_email(
    to: list[str],
    subject: str,
    text: str,
    html: str | None = None,
    reply_to: str | None = None,
    message_id: str | None = None,
) -> None:
    s = get_settings()
    if not smtp_configured():
        raise MailNotConfigured("SMTP is not configured (set SMTP_HOST and SMTP_FROM)")
    msg = EmailMessage()
    msg["From"] = s.smtp_from or s.smtp_username
    msg["To"] = ", ".join(to)
    if reply_to:
        msg["Reply-To"] = _one_line(reply_to)
    if message_id:
        msg["Message-ID"] = message_id
    msg["Subject"] = _one_line(subject)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
        if f"cid:{LOGO_CID}" in html:
            # Inline (CID) image: shows in Gmail/Outlook without the app being reachable from the internet.
            msg.get_body(("html",)).add_related(
                logo_png(), "image", "png", cid=f"<{LOGO_CID}>", filename="ledger.png", disposition="inline"
            )
    await aiosmtplib.send(
        msg,
        hostname=s.smtp_host,
        port=s.smtp_port,
        username=s.smtp_username,
        password=s.smtp_password.get_secret_value() if s.smtp_password else None,
        use_tls=s.smtp_security == "ssl",
        start_tls=True if s.smtp_security == "starttls" else False,
        timeout=30,
    )
