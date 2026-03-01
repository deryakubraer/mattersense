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
    client_driver_num: str,
    defendant_driver_num: str,
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

    # Build explicit driver-number mapping so GPT never confuses the parties
    driver_mapping = ""
    if client_driver_num:
        driver_mapping += f'- "Driver of Vehicle #{client_driver_num}" in the description refers to our client {client_name}\n'
    if defendant_driver_num:
        driver_mapping += f'- "Driver of Vehicle #{defendant_driver_num}" in the description refers to the opposing party {defendant_name}\n'

    prompt = f"""You are a paralegal at Richards & Law, a New York personal injury law firm.
Write the narrative body of a warm, personal email to a new client. Here is the context:

- Client name: {client_name} (address them as {first_name})
- Defendant: {defendant_name}
- Date of accident: {date_of_accident}
- Accident description (from the police report): {accident_description}
{driver_mapping}
Guidelines:
- Open with "Hello {first_name},"
- Write in a warm, empathetic, human tone — not robotic or template-sounding
- In 1–2 sentences, describe the accident naturally, using the real names ({client_name} and {defendant_name}) instead of "Driver of Vehicle #1/2"
- Acknowledge any complexity or disputed facts if present
- Tell the client that their Retainer Agreement is attached and ask them to review it before the consultation — use **bold** for "Retainer Agreement"
- End with a short sentence telling them to click the button below to book their {season_label} consultation
- Do NOT include a booking link or button — that will be added separately
- Do NOT include any closing line, sign-off, or signature (no "Take care", "Best", "Sincerely", "Warm regards", "[Your Name]", etc.) — the signature is added separately
- 3–4 short paragraphs max
- Return plain text only — no HTML tags, no markdown except **bold**, no subject line"""

    client = OpenAI(api_key=openai_api_key)
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=500,
    )
    plain_text = response.choices[0].message.content.strip()

    # Convert plain-text paragraphs → HTML <p> tags; convert **word** → <strong>word</strong>
    import re
    def to_html_para(text: str) -> str:
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        return f"  <p>{text}</p>"

    paragraphs = [p.strip() for p in plain_text.split("\n\n") if p.strip()]
    html_paragraphs = "\n\n".join(to_html_para(p) for p in paragraphs)

    body = f"""<html>
<body style="font-family: Arial, sans-serif; line-height: 1.7; color: #2c2c2c; max-width: 640px; margin: auto; padding: 24px;">

{html_paragraphs}

  <p style="text-align: center; margin: 32px 0;">
    <a href="{scheduling_link}"
       style="background-color: #1a3c5e; color: #ffffff; padding: 14px 28px;
              text-decoration: none; border-radius: 5px; font-weight: bold;
              font-size: 15px; display: inline-block;">
      📅 Book Your {season_label.title()} Consultation
    </a>
  </p>

  <p>Warm regards,</p>
  <p>
    <strong>The Team at Richards &amp; Law</strong><br>
    Andrew Richards, Esq.<br>
    Richards &amp; Law — New York, NY
  </p>

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
