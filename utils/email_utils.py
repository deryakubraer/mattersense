import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import date

from openai import OpenAI


INOFFICE_LINK = "https://calendly.com/swans-santiago-p/summer-spring"
VIRTUAL_LINK  = "https://calendly.com/swans-santiago-p/winter-autumn"


def get_scheduling_link() -> tuple[str, str]:
    """Return (url, label) based on the current month."""
    month = date.today().month
    if 3 <= month <= 8:
        return INOFFICE_LINK, "in-office"
    return VIRTUAL_LINK, "virtual"


def _extract_first_name(client_name: str) -> str:
    """
    Handle both 'Last, First' (police report format) and 'First Last'.
    Returns just the first name.
    """
    if "," in client_name:
        after_comma = client_name.split(",", 1)[1].strip()
        return after_comma.split()[0] if after_comma else client_name.split(",")[0].strip()
    return client_name.split()[0] if client_name else "there"


def compose_client_email(
    client_name: str,
    defendant_name: str,
    date_of_accident: str,
    accident_description: str,
    openai_api_key: str,
) -> tuple[str, str]:
    """
    Generate a personalized client email via GPT-4o and wrap it in clean HTML.
    Returns (subject, html_body).
    """
    scheduling_link, season_label = get_scheduling_link()
    first_name = _extract_first_name(client_name)

    subject = "Richards & Law – Your Case & Next Steps"

    prompt = f"""You are a paralegal at Richards & Law, a New York personal injury law firm.
Write a warm, personal email to a new client. Here is the context:

- Client name: {client_name} (address them as {first_name})
- Defendant: {defendant_name}
- Date of accident: {date_of_accident}
- Accident description (from the police report): {accident_description}
- A Retainer Agreement PDF is attached to the email
- Booking link for their {season_label} consultation: {scheduling_link}

Guidelines:
- Open with "Hello {first_name},"
- Write in a warm, empathetic, human tone — not robotic or template-sounding
- In 1–2 sentences, describe the accident naturally using the real names instead of "Driver of Vehicle #1/2"
- Acknowledge any complexity or disputed facts if present
- Ask them to review the attached Retainer Agreement before the consultation
- Include the booking link as a plain hyperlink (no button — just a line like "You can book your appointment here: <url>")
- Close warmly signed by "The Team at Richards & Law"
- 4–5 short paragraphs max
- Return plain text only — no HTML, no markdown, no subject line"""

    client = OpenAI(api_key=openai_api_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=600,
    )
    plain_text = response.choices[0].message.content.strip()

    # Convert plain-text paragraphs → HTML <p> tags
    paragraphs = [p.strip() for p in plain_text.split("\n\n") if p.strip()]
    html_paragraphs = "\n\n".join(f"  <p>{p}</p>" for p in paragraphs)

    body = f"""<html>
<body style="font-family: Arial, sans-serif; line-height: 1.7; color: #2c2c2c; max-width: 640px; margin: auto; padding: 24px;">

{html_paragraphs}

</body>
</html>"""

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
