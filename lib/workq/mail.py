"""Sending the digest. Every route is a Mailer, and build_services() always wraps it in
AllowedDomainsMailer, so no route can send to an address outside digest.allowed_domains."""

import email.utils
import re
import shlex
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from pathlib import Path

from .graph import GraphError
from .interfaces import MailError

_DOMAIN = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}")


def check_addresses(raw, allowed):
    """The plain addresses in raw ("a@x, B <b@y>"), each checked against allowed.
    Returns (addresses, None) or (None, the reason nothing may be sent). All or nothing."""
    if not allowed:
        return None, ("digest.allowed_domains is empty, so no mail may be sent. List the "
                      "domains mail may go to, e.g. allowed_domains: [company.com.au]")
    if isinstance(raw, (list, tuple)):
        raw = ", ".join(str(x) for x in raw)
    addresses, refused = [], []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        _name, addr = email.utils.parseaddr(part)
        local, at, domain = addr.rpartition("@")
        domain = domain.lower().rstrip(".")
        # Check exactly what will be sent: one plain address, nothing parsed away.
        if (not at or addr.count("@") != 1 or not _LOCAL_PART.fullmatch(local)
                or ".." in local or not _DOMAIN.fullmatch(domain)
                or (addr not in part) or any(c in part for c in "\r\n\0")):
            refused.append(f"{part!r} is not a plain email address")
        elif domain not in allowed:
            refused.append(f"{addr} is not in an allowed domain")
        else:
            addresses.append(f"{local}@{domain}")
    if refused:
        return None, ("mail not sent: " + "; ".join(refused)
                      + f" (digest.allowed_domains: {', '.join(allowed)})")
    if not addresses:
        return None, "digest.to holds no email address"
    return addresses, None


class AllowedDomainsMailer:
    """Checks the addresses, then passes only the checked ones to the real mailer."""

    def __init__(self, inner, allowed):
        self.inner = inner
        self.allowed = list(allowed or [])

    def send(self, recipients, subject, body):
        addresses, refused = check_addresses(recipients, self.allowed)
        if refused:
            raise MailError("invalid", refused)
        self.inner.send(addresses, subject, body)


def build_message(recipients, sender, subject, body, date):
    msg = EmailMessage()
    msg["To"] = ", ".join(recipients)      # only the checked addresses, never the raw setting
    msg["From"] = sender
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(date)
    msg.set_content(body)
    return msg


class SmtpMailer:
    """Built in: needs no mail program on the machine."""

    def __init__(self, smtp, sender, date):
        self.smtp = smtp
        self.sender = sender
        self.date = date

    def send(self, recipients, subject, body):
        smtp = self.smtp
        encrypted = bool(smtp.get("ssl")) or bool(smtp.get("starttls", True))
        local = str(smtp["host"]).lower() in ("localhost", "127.0.0.1", "::1")
        if smtp.get("user"):
            if not smtp.get("password_file"):
                raise MailError("invalid", "digest.smtp.user is set but digest.smtp.password_file is not")
            if not encrypted and not local:
                raise MailError("invalid", "refusing to send the mail password unencrypted: set "
                                           "digest.smtp.starttls or digest.smtp.ssl to true, or remove "
                                           "digest.smtp.user")
        msg = build_message(recipients, self.sender, subject, body, self.date())
        try:
            port = int(smtp.get("port") or 587)
            if smtp.get("ssl"):
                server = smtplib.SMTP_SSL(smtp["host"], port, timeout=60, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(smtp["host"], port, timeout=60)
            with server:
                if smtp.get("starttls", True) and not smtp.get("ssl"):
                    server.starttls(context=ssl.create_default_context())
                if smtp.get("user"):
                    server.login(smtp["user"], Path(smtp["password_file"]).read_text().strip())
                server.send_message(msg, to_addrs=recipients)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError("network", f"SMTP to {smtp['host']}: {exc}")


class CommandMailer:
    """Any program that reads a whole message on stdin (sendmail -t, msmtp -t)."""

    def __init__(self, command, sender, date):
        self.command = command
        self.sender = sender
        self.date = date

    def send(self, recipients, subject, body):
        msg = build_message(recipients, self.sender, subject, body, self.date())
        try:
            proc = subprocess.run(shlex.split(self.command), input=msg.as_string(), text=True,
                                  capture_output=True)
        except OSError as exc:
            raise MailError("other", f"{self.command}: {exc}")
        if proc.returncode != 0:
            raise MailError("other", f"{self.command}: "
                            f"{(proc.stderr or proc.stdout).strip() or 'exit %d' % proc.returncode}")


class GraphMailer:
    """Sends as the signed-in account (Mail.Send), keeping a copy in its Sent Items."""

    def __init__(self, client):
        self.client = client

    def send(self, recipients, subject, body):
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in recipients],
        }
        try:
            self.client.call("POST", "/me/sendMail", {"message": message, "saveToSentItems": True})
        except GraphError as exc:
            raise MailError(exc.kind, f"Graph mail: {exc}") from None
