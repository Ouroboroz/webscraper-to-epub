import email
import email.policy
import os

import pytest

from epub_scraper.epub_writer import build_epub
from epub_scraper.mailer import (MailConfig, MailConfigError, MailSendError,
                                  SanityCheckError, load_mail_config,
                                  sanity_check_epub, send_epub_to_kindle,
                                  send_failure_alert)
from fakes import FakeSMTP

_MAIL_ENV_VARS = ["EPUB_MAIL_SMTP_HOST", "EPUB_MAIL_SMTP_PORT", "EPUB_MAIL_SMTP_USER",
                  "EPUB_MAIL_SMTP_PASSWORD", "EPUB_MAIL_FROM_ADDR", "EPUB_MAIL_KINDLE_ADDR",
                  "EPUB_MAIL_ALERT_ADDR", "EPUB_MAIL_SMTP_USE_SSL"]


@pytest.fixture(autouse=True)
def _clear_mail_env(monkeypatch):
    for name in _MAIL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _write_dotenv(path, **overrides):
    values = {
        "EPUB_MAIL_SMTP_HOST": "smtp.gmail.com", "EPUB_MAIL_SMTP_PORT": "587",
        "EPUB_MAIL_SMTP_USER": "user@gmail.com", "EPUB_MAIL_SMTP_PASSWORD": "secret",
        "EPUB_MAIL_KINDLE_ADDR": "name_1234@kindle.com", "EPUB_MAIL_ALERT_ADDR": "user@gmail.com",
    }
    values.update(overrides)
    with open(path, "w", encoding="utf-8") as f:
        for key, value in values.items():
            f.write(f"{key}={value}\n")


def _config(**overrides):
    values = dict(smtp_host="smtp.gmail.com", smtp_port=587, smtp_user="user@gmail.com",
                  smtp_password="secret", from_addr="user@gmail.com",
                  kindle_addr="name_1234@kindle.com", alert_addr="user@gmail.com",
                  smtp_use_ssl=False)
    values.update(overrides)
    return MailConfig(**values)


# -- load_mail_config -------------------------------------------------------------

def test_load_mail_config_env_only(monkeypatch, tmp_path):
    path = str(tmp_path / ".env")  # deliberately does not exist
    monkeypatch.setenv("EPUB_MAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("EPUB_MAIL_SMTP_PORT", "465")
    monkeypatch.setenv("EPUB_MAIL_SMTP_USER", "u@example.com")
    monkeypatch.setenv("EPUB_MAIL_SMTP_PASSWORD", "pw")
    monkeypatch.setenv("EPUB_MAIL_KINDLE_ADDR", "k@kindle.com")
    monkeypatch.setenv("EPUB_MAIL_ALERT_ADDR", "a@example.com")

    config = load_mail_config(path)
    assert config.smtp_host == "smtp.example.com"
    assert config.smtp_port == 465
    assert config.from_addr == "u@example.com"  # defaulted from smtp_user


def test_load_mail_config_dotenv_file_only(tmp_path):
    path = str(tmp_path / ".env")
    _write_dotenv(path)
    config = load_mail_config(path)
    assert config.smtp_host == "smtp.gmail.com"
    assert config.kindle_addr == "name_1234@kindle.com"


def test_load_mail_config_env_overrides_dotenv_per_field(monkeypatch, tmp_path):
    path = str(tmp_path / ".env")
    _write_dotenv(path, EPUB_MAIL_SMTP_HOST="file-host")
    monkeypatch.setenv("EPUB_MAIL_SMTP_HOST", "env-host")

    config = load_mail_config(path)
    assert config.smtp_host == "env-host"
    assert config.smtp_user == "user@gmail.com"  # still from the .env file


def test_load_mail_config_from_addr_defaults_to_smtp_user(tmp_path):
    path = str(tmp_path / ".env")
    _write_dotenv(path)
    config = load_mail_config(path)
    assert config.from_addr == config.smtp_user


def test_load_mail_config_missing_required_field_names_env_var(tmp_path):
    path = str(tmp_path / ".env")
    with open(path, "w") as f:
        f.write("EPUB_MAIL_SMTP_HOST=h\nEPUB_MAIL_SMTP_PORT=587\n"
                 "EPUB_MAIL_SMTP_USER=u\nEPUB_MAIL_SMTP_PASSWORD=p\n"
                 "EPUB_MAIL_ALERT_ADDR=a@example.com\n")  # kindle_addr missing

    with pytest.raises(MailConfigError) as exc_info:
        load_mail_config(path)
    msg = str(exc_info.value)
    assert "EPUB_MAIL_KINDLE_ADDR" in msg


def test_load_mail_config_no_file_no_env_raises(tmp_path):
    path = str(tmp_path / ".env")
    with pytest.raises(MailConfigError):
        load_mail_config(path)


def test_load_mail_config_comments_and_blank_lines_ignored(tmp_path):
    path = str(tmp_path / ".env")
    with open(path, "w") as f:
        f.write("# this is a comment\n\n")
        f.write("EPUB_MAIL_SMTP_HOST=h\n")
        f.write("not a valid line without equals\n")
        f.write("EPUB_MAIL_SMTP_PORT=587\n")
        f.write("EPUB_MAIL_SMTP_USER=u\nEPUB_MAIL_SMTP_PASSWORD=p\n")
        f.write("EPUB_MAIL_KINDLE_ADDR=k@kindle.com\nEPUB_MAIL_ALERT_ADDR=a@example.com\n")

    config = load_mail_config(path)  # must not raise on the malformed line
    assert config.smtp_host == "h"


def test_load_mail_config_quoted_value_stripped(tmp_path):
    path = str(tmp_path / ".env")
    _write_dotenv(path, EPUB_MAIL_SMTP_PASSWORD='"a password"')
    assert load_mail_config(path).smtp_password == "a password"


def test_load_mail_config_smtp_use_ssl_string_coercion_from_env(monkeypatch, tmp_path):
    path = str(tmp_path / ".env")
    _write_dotenv(path)
    monkeypatch.setenv("EPUB_MAIL_SMTP_USE_SSL", "true")
    assert load_mail_config(path).smtp_use_ssl is True


def test_load_mail_config_smtp_use_ssl_from_dotenv(tmp_path):
    path = str(tmp_path / ".env")
    _write_dotenv(path, EPUB_MAIL_SMTP_USE_SSL="true")
    assert load_mail_config(path).smtp_use_ssl is True


def test_load_mail_config_smtp_port_env_coerced_to_int(monkeypatch, tmp_path):
    path = str(tmp_path / ".env")
    _write_dotenv(path)
    monkeypatch.setenv("EPUB_MAIL_SMTP_PORT", "2525")
    assert load_mail_config(path).smtp_port == 2525
    assert isinstance(load_mail_config(path).smtp_port, int)


# -- sanity_check_epub ------------------------------------------------------------

def _build(tmp_path, chapters, name="book.epub"):
    out = str(tmp_path / name)
    build_epub("Title", "site", "id", chapters, out)
    return out


def test_sanity_check_epub_passes_with_adequate_content(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>" + "word " * 20 + "</p>")])
    result = sanity_check_epub(path)
    assert result.ok is True
    assert result.chapter_count == 1


def test_sanity_check_epub_fails_below_min_chapters(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>" + "word " * 20 + "</p>")])
    result = sanity_check_epub(path, min_chapters=2)
    assert result.ok is False
    assert "1 chapter" in result.reason


def test_sanity_check_epub_fails_on_too_short_chapter_names_it(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>" + "word " * 20 + "</p>"), ("Ch 2", "<p>hi</p>")])
    result = sanity_check_epub(path, min_chapter_chars=50)
    assert result.ok is False
    assert "chap_0002.xhtml" in result.reason


def test_sanity_check_epub_excludes_nav_document(tmp_path):
    path = _build(tmp_path, [("Unique Words Here", "<p>" + "word " * 20 + "</p>")])
    result = sanity_check_epub(path)
    assert result.chapter_count == 1  # nav.xhtml never counted


def test_sanity_check_epub_nonexistent_path_never_raises(tmp_path):
    result = sanity_check_epub(str(tmp_path / "does-not-exist.epub"))
    assert result.ok is False
    assert "could not open" in result.reason


def test_sanity_check_epub_corrupt_file_never_raises(tmp_path):
    path = tmp_path / "corrupt.epub"
    path.write_bytes(b"not a real epub")
    result = sanity_check_epub(str(path))
    assert result.ok is False


# -- send_epub_to_kindle ------------------------------------------------------------

def test_send_epub_to_kindle_happy_path(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>" + "word " * 20 + "</p>")], name="[Ch 1 - Ch 1] Title.epub")
    fake = FakeSMTP()
    config = _config()

    send_epub_to_kindle(path, "My Novel", config, attachment_name="[Ch 1 - Ch 1] Title.epub",
                         smtp_factory=lambda c: fake)

    assert len(fake.sent) == 1
    from_addr, to_addrs, raw = fake.sent[0]
    assert from_addr == config.from_addr
    assert to_addrs == [config.kindle_addr]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    attachment = next(part for part in msg.iter_attachments())
    assert attachment.get_filename() == "[Ch 1 - Ch 1] Title.epub"


def test_send_epub_to_kindle_attachment_name_defaults_to_basename(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>" + "word " * 20 + "</p>")], name="mybook.epub")
    fake = FakeSMTP()
    send_epub_to_kindle(path, "My Novel", _config(), smtp_factory=lambda c: fake)
    msg = email.message_from_bytes(fake.sent[0][2], policy=email.policy.default)
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "mybook.epub"


def test_send_epub_to_kindle_sanity_failure_raises_and_never_calls_smtp(tmp_path):
    path = _build(tmp_path, [])  # zero chapters -> fails sanity check

    def _factory_should_not_be_called(config):
        raise AssertionError("smtp_factory must not be called when sanity check fails")

    with pytest.raises(SanityCheckError):
        send_epub_to_kindle(path, "My Novel", _config(), smtp_factory=_factory_should_not_be_called)


def test_send_epub_to_kindle_connect_failure_raises_mail_send_error(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>" + "word " * 20 + "</p>")])

    def _factory_raises(config):
        raise ConnectionRefusedError("nope")

    with pytest.raises(MailSendError):
        send_epub_to_kindle(path, "My Novel", _config(), smtp_factory=_factory_raises)


def test_send_epub_to_kindle_sendmail_failure_raises_and_still_quits(tmp_path):
    path = _build(tmp_path, [("Ch 1", "<p>" + "word " * 20 + "</p>")])
    fake = FakeSMTP(fail_on_sendmail=Exception("boom"))

    with pytest.raises(MailSendError):
        send_epub_to_kindle(path, "My Novel", _config(), smtp_factory=lambda c: fake)
    assert fake.quit_called is True


# -- send_failure_alert ------------------------------------------------------------

def test_send_failure_alert_happy_path():
    fake = FakeSMTP()
    config = _config()
    result = send_failure_alert("subject", "message", config, smtp_factory=lambda c: fake)
    assert result is True
    assert len(fake.sent) == 1
    assert fake.sent[0][1] == [config.alert_addr]


def test_send_failure_alert_failure_returns_false_and_warns(capsys):
    fake = FakeSMTP(fail_on_sendmail=Exception("down"))
    result = send_failure_alert("subject", "message", _config(), smtp_factory=lambda c: fake)
    assert result is False
    assert "warning" in capsys.readouterr().out.lower()
