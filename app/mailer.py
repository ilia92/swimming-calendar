"""Send e-mail directly to the recipient's mail server (no provider, no account).

    DirectMailer(sender="Swimming Calendar <noreply@example.com>").send(to, subject, text)

How it works: look up the MX records of the recipient's domain (our own tiny DNS query,
standard library only), connect to the best mail server on port 25, use STARTTLS when it is
offered, and hand over the message. Nothing listens on this machine - it is outgoing only.

Caveat: big providers (Gmail, Outlook...) check that the sending IP is allowed by the sender
domain's SPF record and/or that the mail is DKIM-signed. Without that, mail from a home IP
is usually rejected or lands in spam. See `last_log` for the server's answer.
"""

import random
import smtplib
import socket
import ssl
import struct
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr


def _resolvers():
    out = []
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    out.append(parts[1])
    except OSError:
        pass
    return out + ["1.1.1.1", "8.8.8.8"]


def _read_name(msg, pos):
    labels, jumped, end = [], False, None
    while True:
        n = msg[pos]
        if n == 0:
            pos += 1
            break
        if n & 0xC0 == 0xC0:                       # compression pointer
            if not jumped:
                end = pos + 2
            pos = ((n & 0x3F) << 8) | msg[pos + 1]
            jumped = True
            continue
        labels.append(msg[pos + 1:pos + 1 + n].decode("ascii", "replace"))
        pos += 1 + n
    return ".".join(labels), (end if jumped else pos)


def mx_hosts(domain, timeout=5):
    """[host, ...] best first; falls back to the domain itself (RFC 5321 implicit MX)."""
    qid = random.randint(0, 0xFFFF)
    question = b"".join(bytes([len(p)]) + p.encode("idna") for p in domain.rstrip(".").split(".")) + b"\0"
    packet = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + question + struct.pack(">HH", 15, 1)   # MX, IN
    for server in _resolvers():
        try:
            with socket.socket(socket.AF_INET6 if ":" in server else socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(packet, (server, 53))
                msg = s.recv(4096)
        except OSError:
            continue
        rid, flags, qd, an = struct.unpack(">HHHH", msg[:8])
        if rid != qid or flags & 0x000F:
            continue
        pos = 12
        for _ in range(qd):
            _, pos = _read_name(msg, pos)
            pos += 4
        found = []
        for _ in range(an):
            _, pos = _read_name(msg, pos)
            rtype, _, _, rdlen = struct.unpack(">HHIH", msg[pos:pos + 10])
            pos += 10
            if rtype == 15:
                pref = struct.unpack(">H", msg[pos:pos + 2])[0]
                host, _ = _read_name(msg, pos + 2)
                found.append((pref, host))
            pos += rdlen
        if found:
            return [h for _, h in sorted(found)]
        return [domain]
    return [domain]


class DirectMailer:
    def __init__(self, sender, helo=None, timeout=20):
        """helo: the name we greet servers with. Must be a fully-qualified hostname - ideally the
        reverse-DNS name of our public IP. Defaults to this machine's FQDN, else the sender's domain."""
        self.sender = sender
        fqdn = socket.getfqdn()
        self.helo = helo or (fqdn if "." in fqdn else parseaddr(sender)[1].split("@")[-1])
        self.timeout = timeout
        self.last_log = []

    def send(self, to, subject, text, html=None, attachments=()):
        """attachments: [(filename, bytes, maintype, subtype)]"""
        """Deliver one message. Returns True on success; the conversation is in `last_log`."""
        self.last_log = []
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        from_addr = parseaddr(self.sender)[1]
        msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        for filename, data, maintype, subtype in attachments:
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        rcpt = parseaddr(to)[1]
        for host in mx_hosts(rcpt.split("@")[-1]):
            try:
                with smtplib.SMTP(host, 25, local_hostname=self.helo, timeout=self.timeout) as smtp:
                    smtp.ehlo()
                    if smtp.has_extn("starttls"):
                        smtp.starttls(context=ssl.create_default_context())
                        smtp.ehlo()
                        self.last_log.append("%s: STARTTLS ok" % host)
                    refused = smtp.send_message(msg, from_addr=from_addr, to_addrs=[rcpt])
                    if not refused:
                        self.last_log.append("%s: accepted" % host)
                        return True
                    self.last_log.append("%s: refused %r" % (host, refused))
            except smtplib.SMTPResponseException as e:
                self.last_log.append("%s: %s %s" % (host, e.smtp_code, e.smtp_error.decode("utf-8", "replace")))
                if 500 <= e.smtp_code < 600:
                    return False            # a permanent "no" - other MX hosts will say the same
            except (OSError, smtplib.SMTPException) as e:
                self.last_log.append("%s: %s" % (host, e))
        return False
