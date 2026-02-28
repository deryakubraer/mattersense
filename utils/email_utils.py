import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date


INOFFICE_LINK = "https://calendly.com/swans-santiago-p/summer-spring"
VIRTUAL_LINK  = "https://calendly.com/swans-santiago-p/winter-autumn"


def get_scheduling_link() -> tuple[str, str]:
    """Return (url, label) based on the current month."""
    month = date.today().month
    if 3 <= month <= 8:
        return INOFFICE_LINK, "in-office"
    return VIRTUAL_LINK, "virtual"


def compose_client_email(
    client_name: str,
    defendant_name: str,
    date_of_accident: str,
    accident_description: str,
) -> tuple[str, str]:
    """
    Build the subject and HTML body for the personalized client email.
    Returns (subject, html_body).
    """
    scheduling_link, season_label = get_scheduling_link()

    # Police reports use "Last, First" format — extract the first name correctly
    if "," in client_name:
        after_comma = client_name.split(",", 1)[1].strip()
        first_name = after_comma.split()[0] if after_comma else client_name.split(",")[0].strip()
    else:
        first_name = client_name.split()[0] if client_name else "there"

    # Replace generic "Driver of Vehicle #1 / #2" with the actual party names
    description = accident_description
    description = re.sub(r"Driver of Vehicle #?1", client_name,   description, flags=re.IGNORECASE)
    description = re.sub(r"Driver of Vehicle #?2", defendant_name, description, flags=re.IGNORECASE)

    subject = "Richards & Law – Your Case & Next Steps"

    body = f"""
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.7; color: #2c2c2c; max-width: 640px; margin: auto; padding: 24px;">

  <p>Dear {first_name},</p>

  <p>
    Thank you for reaching out to <strong>Richards &amp; Law</strong>. We understand that the
    accident you experienced on <strong>{date_of_accident}</strong> has been a difficult time —
    {description} — and we want you to know that you have our full support.
  </p>

  <p>
    Our team is committed to fighting for the compensation you deserve. To get started, we
    have prepared your <strong>Retainer Agreement</strong>, which is attached to this email
    as a PDF. Please take a moment to review it before our consultation.
  </p>

  <p>
    We would love to schedule a <strong>{season_label} consultation</strong> with you at your
    earliest convenience. Please click the button below to book a time that works for you:
  </p>

  <p style="text-align: center; margin: 32px 0;">
    <a href="{scheduling_link}"
       style="background-color: #1a3c5e; color: #ffffff; padding: 14px 28px;
              text-decoration: none; border-radius: 5px; font-weight: bold;
              font-size: 15px; display: inline-block;">
      📅 Book Your {season_label.title()} Consultation
    </a>
  </p>

  <p>
    If you have any questions in the meantime, please do not hesitate to reach out directly.
    We look forward to speaking with you soon.
  </p>

  <p>Warm regards,</p>
  <p>
    <strong>The Team at Richards &amp; Law</strong><br>
    Andrew Richards, Esq.<br>
    Richards &amp; Law — New York, NY
  </p>

</body>
</html>
"""
    return subject, body


def send_email(
    gmail_address: str,
    gmail_password: str,
    to_email: str,
    subject: str,
    body_html: str,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
) -> None:
    """Send an HTML email with an optional PDF attachment via Gmail SMTP SSL."""
    msg = MIMEMultipart("mixed")
    msg["From"]    = gmail_address
    msg["To"]      = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(body_html, "html"))

    if attachment_bytes and attachment_filename:
        part = MIMEBase("application", "pdf")
        part.set_payload(attachment_bytes)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{attachment_filename}"',
        )
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_password)
        server.send_message(msg)
