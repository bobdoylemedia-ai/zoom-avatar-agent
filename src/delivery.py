"""Turn a markdown recap into readable files and (optionally) email it.

One shared path so the automatic end-of-meeting delivery and the manual
`src/recap.py` rerun behave identically.

Configured by environment (.env.local):

    NOTES_FORMATS   comma-separated: pdf, docx, or both. Default "pdf".
                    Set to "none" to keep markdown only.
    NOTES_EMAIL_TO  recipient(s). Empty means don't email. See mailer.py for the
                    SMTP settings that go with it.

Everything here is best-effort by design: the markdown recap and the raw
transcript are already on disk before this runs, so a missing PDF library or a
wrong SMTP password must never look like a lost meeting.
"""

from __future__ import annotations

import logging
import os
import pathlib

import export
import mailer

logger = logging.getLogger("zoom-avatar.delivery")


def formats() -> list[str]:
    raw = (os.getenv("NOTES_FORMATS") or "pdf").strip().lower()
    if raw in {"none", "off", ""}:
        return []
    if raw == "both":
        return ["pdf", "docx"]
    wanted, unknown = [], []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        (wanted if part in export.FORMATS else unknown).append(part)
    if unknown:
        logger.warning(
            "Ignoring unknown NOTES_FORMATS value(s): %s (valid: pdf, docx, both, none)",
            ", ".join(unknown),
        )
    return wanted


def _email_body(meeting_url: str, bot_name: str, turn_count: int, has_attachments: bool) -> str:
    lines = [
        f"{bot_name} attended a meeting on your behalf and took notes.",
        "",
        f"Meeting : {meeting_url}",
        f"Turns   : {turn_count}",
        "",
    ]
    lines.append(
        "The notes are attached." if has_attachments
        else "The notes could not be attached; they are in the meetings folder on your PC."
    )
    lines += [
        "",
        "Note: meeting audio has no speaker labels, so the notes record what was",
        "said rather than who said it. Names appear only where someone was named",
        "out loud or a line came from meeting chat.",
    ]
    return "\n".join(lines)


def deliver(
    recap_md_path: pathlib.Path,
    *,
    meeting_url: str = "",
    bot_name: str = "AI avatar",
    turn_count: int = 0,
    email: bool = True,
) -> dict:
    """Render the recap to the configured formats and email it if configured.

    Returns {"files": [...], "emailed_to": [...]} describing what happened.
    Never raises for an expected failure -- it logs and carries on.
    """
    result: dict = {"files": [], "emailed_to": []}

    try:
        md = recap_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read recap %s: %s", recap_md_path, exc)
        return result

    title = f"Meeting notes - {recap_md_path.stem}"
    for fmt in formats():
        try:
            path = export.FORMATS[fmt](md, recap_md_path.with_suffix(f".{fmt}"), title=title)
            result["files"].append(path)
            logger.info("Wrote %s", path)
        except Exception as exc:  # noqa: BLE001 - one bad format shouldn't stop the rest
            logger.warning("Could not write %s: %s", fmt, exc)

    if not email:
        return result
    if not mailer.recipients():
        logger.info("NOTES_EMAIL_TO not set; skipping email.")
        return result

    # Attach the rendered files, plus the markdown as a readable fallback.
    attachments = list(result["files"]) + [recap_md_path]
    subject = f"Meeting notes - {bot_name}"
    try:
        result["emailed_to"] = mailer.send_notes(
            subject,
            _email_body(meeting_url, bot_name, turn_count, bool(result["files"])),
            attachments,
        )
    except mailer.MailNotConfigured as exc:
        logger.warning("%s", exc)
    except Exception as exc:  # noqa: BLE001 - a mail failure is not a lost meeting
        logger.warning("Emailing notes failed: %s: %s", type(exc).__name__, exc)

    return result
