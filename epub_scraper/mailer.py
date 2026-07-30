"""Send a built .epub to a Kindle's Send-to-Kindle email address, plus a
failure-alert email if a send goes wrong. Stdlib-only (smtplib + email).
"""

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import NamedTuple, Optional

from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT

DEFAULT_MAIL_CONFIG_PATH = ".env"
DEFAULT_MIN_CHAPTERS = 1
DEFAULT_MIN_CHAPTER_CHARS = 50

_ENV_PREFIX = "EPUB_MAIL_"
_ALL_FIELDS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password",
               "from_addr", "kindle_addr", "alert_addr", "smtp_use_ssl")
_REQUIRED_FIELDS = ("smtp_host", "smtp_port", "smtp_user", "smtp_password",
                     "kindle_addr", "alert_addr")


class MailConfigError(Exception):
    """Kindle-mail configuration is missing or invalid."""


class SanityCheckError(Exception):
    """An epub failed its pre-send sanity check -- nothing was sent."""


class MailSendError(Exception):
    """SMTP connect/login/send failed."""


@dataclass(frozen=True)
class MailConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_addr: str
    kindle_addr: str
    alert_addr: str
    smtp_use_ssl: bool = False


def _parse_dotenv(path):
    """Minimal .env parser: KEY=VALUE per line, '#' comments and blank lines
    skipped, matching single/double quotes around a value stripped. No shell
    interpolation, no `export` prefix -- just enough for local secrets, read
    directly by this process so it works identically whether invoked
    interactively or from cron (unlike relying on the shell to export them)."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def load_mail_config(path=DEFAULT_MAIL_CONFIG_PATH):
    """Per-field env-var-first, local-.env-file-fallback. Cron does not inherit
    the interactive shell's exported env vars, so the file is what makes this
    reliable under a cron job; real env vars still take priority when set,
    for manual/test overrides."""
    dotenv_values = _parse_dotenv(path)

    values = {}
    for name in _ALL_FIELDS:
        env_key = _ENV_PREFIX + name.upper()
        env_val = os.environ.get(env_key)
        if env_val is not None:
            values[name] = env_val
        elif env_key in dotenv_values:
            values[name] = dotenv_values[env_key]

    missing = [f for f in _REQUIRED_FIELDS if not values.get(f)]
    if missing:
        lines = [f"  {_ENV_PREFIX}{f.upper()}  (env var, or a line in {path!r})" for f in missing]
        raise MailConfigError(
            "Missing Kindle-mail configuration:\n" + "\n".join(lines) +
            f"\n\nSet these as environment variables, or add them to {path} (see README.md).")

    values.setdefault("from_addr", values["smtp_user"])
    values["smtp_port"] = int(values["smtp_port"])
    ssl_val = values.get("smtp_use_ssl", False)
    values["smtp_use_ssl"] = (ssl_val.strip().lower() in ("1", "true", "yes", "on")
                               if isinstance(ssl_val, str) else bool(ssl_val))

    return MailConfig(**values)


class SanityResult(NamedTuple):
    ok: bool
    reason: Optional[str]
    chapter_count: int


def sanity_check_epub(epub_path, min_chapters=DEFAULT_MIN_CHAPTERS,
                       min_chapter_chars=DEFAULT_MIN_CHAPTER_CHARS):
    """Refuse-to-send guard: reads epub_path back (same read_epub(..., options=
    {'ignore_ncx': True}) + ITEM_DOCUMENT + 'chap_' prefix filter as textsearch.py,
    so nav.xhtml is never miscounted as a chapter) and checks it isn't obviously
    broken. Never raises -- always returns a SanityResult."""
    try:
        book = epub.read_epub(epub_path, options={"ignore_ncx": True})
    except Exception as e:
        return SanityResult(False, f"could not open epub: {e}", 0)

    chapters = [item for item in book.get_items_of_type(ITEM_DOCUMENT)
                if item.get_name().startswith("chap_")]
    if len(chapters) < min_chapters:
        return SanityResult(False,
            f"only {len(chapters)} chapter(s) found (minimum {min_chapters})", len(chapters))

    for item in chapters:
        text = BeautifulSoup(item.get_content(), "html.parser").get_text(strip=True)
        if len(text) < min_chapter_chars:
            return SanityResult(False,
                f"chapter {item.get_name()!r} has only {len(text)} character(s) of text "
                f"(minimum {min_chapter_chars}) -- possible parse failure", len(chapters))

    return SanityResult(True, None, len(chapters))


def _default_smtp_factory(config):
    if config.smtp_use_ssl:
        conn = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30)
    else:
        conn = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
        conn.starttls()
    conn.login(config.smtp_user, config.smtp_password)
    return conn


def _send_email(config, to_addr, subject, body, *, attachment_path=None,
                 attachment_name=None, smtp_factory=_default_smtp_factory):
    """Shared low-level sender: plain-text body plus an optional epub attachment.
    Wraps any connect/login/send failure as MailSendError; always attempts
    conn.quit() regardless of outcome."""
    msg = EmailMessage()
    msg["From"] = config.from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment_path:
        with open(attachment_path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="application", subtype="epub+zip",
                            filename=attachment_name or os.path.basename(attachment_path))

    try:
        conn = smtp_factory(config)
    except Exception as e:
        raise MailSendError(f"could not connect/authenticate to "
                             f"{config.smtp_host}:{config.smtp_port}: {e}") from e
    try:
        conn.sendmail(config.from_addr, [to_addr], msg.as_bytes())
    except Exception as e:
        raise MailSendError(f"failed to send email to {to_addr}: {e}") from e
    finally:
        try:
            conn.quit()
        except Exception:
            pass


def send_epub_to_kindle(epub_path, subject, config, *, attachment_name=None,
                         smtp_factory=_default_smtp_factory,
                         min_chapters=DEFAULT_MIN_CHAPTERS,
                         min_chapter_chars=DEFAULT_MIN_CHAPTER_CHARS):
    """Sanity-checks epub_path first -- raises SanityCheckError, sends nothing,
    if it fails. Otherwise emails it as an attachment to config.kindle_addr.
    Raises MailSendError on any SMTP failure. Caller owns all library.json
    bookkeeping and triggering send_failure_alert."""
    result = sanity_check_epub(epub_path, min_chapters=min_chapters,
                                min_chapter_chars=min_chapter_chars)
    if not result.ok:
        raise SanityCheckError(result.reason)

    body = f"{subject} -- {result.chapter_count} chapter(s). Sent by epub_scraper."
    _send_email(config, config.kindle_addr, subject=subject, body=body,
                attachment_path=epub_path, attachment_name=attachment_name,
                smtp_factory=smtp_factory)


def send_failure_alert(subject, message, config, smtp_factory=_default_smtp_factory):
    """Best-effort notice to config.alert_addr. Never raises -- if the alert
    itself fails to send, prints a warning and returns False, so a double
    failure (Kindle send AND alert send) can't abort the caller's loop."""
    try:
        _send_email(config, config.alert_addr, subject, message, smtp_factory=smtp_factory)
        return True
    except MailSendError as e:
        print(f"warning: failed to send failure-alert email: {e}")
        return False
