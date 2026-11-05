"""SMTP transport shared by API/worker code. Keycloak owns identity emails."""
from email.message import EmailMessage
from email.utils import parseaddr
import smtplib
import ssl
from app import settings as s


def address(value):
    # One mailbox only; reject header injection and ambiguous recipient lists.
    if any(char in value for char in "\r\n,;"):
        raise ValueError("Expected one email mailbox")
    _, mailbox = parseaddr(value)
    if not mailbox or "@" not in mailbox:
        raise ValueError("Expected one email mailbox")
    return mailbox


def send_email(recipient, subject, text):
    if not s.SMTP_HOST:
        raise RuntimeError("Application SMTP is not configured")
    sender, recipient = address(s.SMTP_FROM), address(recipient)
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = s.SMTP_FROM, recipient, subject
    message.set_content(text)
    if s.SMTP_SECURITY == "tls":
        client = smtplib.SMTP_SSL(s.SMTP_HOST, s.SMTP_PORT, timeout=10, context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=10)
    with client:
        if s.SMTP_SECURITY == "starttls":
            client.starttls(context=ssl.create_default_context())
        if s.SMTP_USERNAME:
            client.login(s.SMTP_USERNAME, s.SMTP_PASSWORD)
        client.send_message(message, from_addr=sender, to_addrs=[recipient])
    # Call synchronous SMTP from a worker or thread pool, not an async event loop.


if __name__ == "__main__":
    if s.ENVIRONMENT != "local" or s.SMTP_HOST != "mailpit":
        raise SystemExit("Demo email is restricted to the local Mailpit service")
    send_email("demo@example.test", "Workbench application email", "Synthetic app email. Open the workbench at " + s.PUBLIC_URL + "/")
    print("Sent synthetic application email to local Mailpit")
