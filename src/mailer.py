"""Email a meeting recap as an attachment, over SMTP.

Configured entirely by environment (.env.local):

    NOTES_EMAIL_TO      recipient(s), comma-separated. Empty = emailing off.
    NOTES_EMAIL_FROM    From address. Defaults to SMTP_USER.
    SMTP_HOST           e.g. smtp.gmail.com
    SMTP_PORT           587 for STARTTLS (default), 465 for implicit TLS
    SMTP_USER           the mailbox to authenticate as
    SMTP_PASSWORD       app password -- see below
    SMTP_TLS            "starttls" (default), "ssl", or "none"

On Gmail an ordinary account password will NOT work: you need a 16-character
App Password from the Google Account security page, with 2-Step Verification on.
Put it in .env.local yourself -- it is gitignored, and nothing here logs it.

Nothing is sent unless NOTES_EMAIL_TO is set, so the whole feature is opt-in by
configuration rather than by a flag someone has to remember.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import pathlib
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger("zoom-avatar.mailer")


class MailNotConfigured(RuntimeError):
    """Raised when emailing was requested but the settings are incomplete."""


def recipients() -> list[str]:
    raw = os.getenv("NOTES_EMAIL_TO", "")
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


def is_configured() -> bool:
    return bool(recipients()) and bool(os.getenv("SMTP_HOST")) and bool(os.getenv("SMTP_USER"))


def _missing() -> list[str]:
    needed = {
        "NOTES_EMAIL_TO": ",".join(recipients()),
        "SMTP_HOST": os.getenv("SMTP_HOST", ""),
        "SMTP_USER": os.getenv("SMTP_USER", ""),
        "SMTP_PASSWORD": os.getenv("SMTP_PASSWORD", ""),
    }
    return [k for k, v in needed.items() if not v]


def _attach(msg: EmailMessage, path: pathlib.Path) -> None:
    ctype, encoding = mimetypes.guess_type(path.name)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, _, subtype = ctype.partition("/")
    msg.add_attachment(
        path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
    )


def send_notes(
    subject: str,
    body: str,
    attachments: list[pathlib.Path] | None = None,
) -> list[str]:
    """Send one email. Returns the recipient list on success.

    Raises MailNotConfigured if settings are incomplete, or smtplib errors
    through to the caller so the reason reaches the log.
    """
    missing = _missing()
    if missing:
        raise MailNotConfigured(
            "Email not sent -- missing " + ", ".join(missing) + " in .env.local"
        )

    to = recipients()
    host = os.environ["SMTP_HOST"]
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    mode = os.getenv("SMTP_TLS", "starttls").strip().lower()
    port = int(os.getenv("SMTP_PORT") or (465 if mode == "ssl" else 587))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("NOTES_EMAIL_FROM", "").strip() or user
    msg["To"] = ", ".join(to)
    msg.set_content(body)

    for path in attachments or []:
        if path and path.is_file():
            _attach(msg, path)
        elif path:
            logger.warning("Attachment missing, skipping: %s", path)

    context = ssl.create_default_context()
    if mode == "ssl":
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            if mode != "none":
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)

    logger.info("Notes emailed to %s", ", ".join(to))
    return to


def _self_test() -> int:
    """`uv run python src/mailer.py` -- send one test email to NOTES_EMAIL_TO.

    Use this to prove the SMTP settings work before relying on it after a real
    meeting, rather than discovering a bad app password in the shutdown log.
    """
    import pathlib as _pathlib

    from dotenv import load_dotenv

    root = _pathlib.Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env.local")
    load_dotenv(root / ".env")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    missing = _missing()
    if missing:
        print("Not configured. Add these to .env.local:")
        for key in missing:
            print(f"  {key}=")
        print("\nGmail needs a 16-character App Password, not your account password:")
        print("  https://myaccount.google.com/apppasswords")
        return 1

    print(f"host      : {os.getenv('SMTP_HOST')}:{os.getenv('SMTP_PORT') or '(default)'}")
    print(f"user      : {os.getenv('SMTP_USER')}")
    print(f"tls       : {os.getenv('SMTP_TLS', 'starttls')}")
    print(f"to        : {', '.join(recipients())}")
    print()
    try:
        sent = send_notes(
            "Zoom Avatar Agent - test email",
            "If you are reading this, the avatar can email you meeting notes.\n"
            "Nothing else to do.\n",
        )
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic entry point
        print(f"FAILED: {type(exc).__name__}: {exc}")
        if "auth" in str(exc).lower() or "credential" in str(exc).lower():
            print("\nThat looks like an auth failure. On Gmail you need an App")
            print("Password (2-Step Verification must be on), not the account password.")
        return 1
    print(f"Sent to {', '.join(sent)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
