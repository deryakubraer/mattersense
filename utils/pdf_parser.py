import base64
import json
import re
import fitz  # PyMuPDF
from openai import OpenAI

EXTRACTION_PROMPT = """You are a legal data extraction specialist for police accident reports.

Your task is to carefully read the FULL document (including all pages) and extract exact values.

You will receive:
  1. A ZOOMED HEADER CROP — a close-up of the top of page 1 showing the accident date,
     No. of Vehicles, No. Injured, and No. Killed fields side-by-side. Use this image for
     date_of_accident and number_of_injured. These three count fields sit next to each other;
     read each one from its own labelled box — do NOT mix them up.
  2. FULL PAGE IMAGES — all pages at high resolution for everything else.
  3. REFERENCE VALUES — character-perfect plate numbers and dates from the PDF text layer.

Pay special attention to:
- DATES: The accident date is in the header crop (Month / Day / Year boxes). Return MM/DD/YYYY.
- LICENSE PLATES: Look in the "Vehicle" or "Registration" section for each driver.
  Read the plate from the page image. Copy it character by character — pay close attention
  to easily confused pairs: O vs 0, I vs 1, Z vs 2, B vs 8, S vs 5.
- DRIVER NAMES: Look in "Driver Information" or "Vehicle Operator" sections. Use the full legal name.
- NUMBER OF INJURED: Read the value from the "No. Injured" box in the HEADER CROP — NOT the
  "No. of Vehicles" box (which is immediately to its left) and NOT "No. Killed" (to its right).
  Copy the integer from the "No. Injured" box exactly. If blank, return 0.

Return ONLY this JSON object (no markdown, no explanation):

{
    "parties": [
        {
            "name": "Full legal name exactly as printed",
            "role": "DRIVER 1, DRIVER 2, PEDESTRIAN, etc.",
            "vehicle_plate": "Exact plate number as printed, or null",
            "vehicle_description": "Year Make Model, or null"
        }
    ],
    "date_of_accident": "MM/DD/YYYY — copy the exact date from the report",
    "accident_location": "Full street address or intersection",
    "accident_description": "1-2 sentences describing what happened",
    "number_of_injured": 0,
    "report_number": "Report/case number, or null"
}

Critical rules:
- Do NOT guess or infer dates — copy them exactly as written.
- Do NOT guess or infer plate numbers — copy them exactly character by character.
- Extract ALL drivers/parties listed in the report.
- number_of_injured comes from the "No. Injured" box in the header crop, never from reference values.
- Return ONLY the raw JSON object.
"""

# Tokens that look like plates but are common non-plate words
_PLATE_STOPWORDS = {
    'MV104AN', '104AN', 'VEHICLE', 'REPORT', 'POLICE', 'SECTOR', 'SEDAN',
    'COUPE', 'TRUCK', 'AVENUE', 'STREET', 'PATROL', 'REVIEW',
    'BICYCLIST', 'PEDESTRIAN', 'OTHER', 'AMENDED',
}

# Top fraction of the first page that contains the header row
# (accident date · No. of Vehicles · No. Injured · No. Killed).
# 20% gives enough room to survive court filing stamps (e.g. NYSCEF)
# that can occupy the top 5-8% before the form itself begins.
_HEADER_CROP_PCT = 0.20


def _extract_plate_candidates(text: str) -> list[str]:
    """
    Pull tokens from embedded text that look like license plates.
    Requirements: 5–8 chars, uppercase letters/digits only, ≥2 letters, ≥1 digit.
    Scanned police reports produce garbled OCR; we only want alphanumeric plate-like tokens.
    """
    candidates = re.findall(r'\b[A-Z0-9]{5,8}\b', text)
    plates = [
        t for t in candidates
        if len(re.findall(r'[A-Z]', t)) >= 2
        and re.search(r'[0-9]', t)
        and t not in _PLATE_STOPWORDS
    ]
    return list(dict.fromkeys(plates))  # deduplicate, preserve order


def _extract_date_candidates(text: str) -> list[str]:
    """Pull MM/DD/YYYY date strings from embedded text (plausible years only)."""
    all_dates = re.findall(r'\b\d{1,2}/\d{1,2}/\d{4}\b', text)
    return list(dict.fromkeys(
        d for d in all_dates if 1990 <= int(d.split('/')[-1]) <= 2099
    ))


def pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract embedded text from PDF using PyMuPDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = [page.get_text("text") for page in doc]
    doc.close()
    return "\n\n".join(pages).strip()


def pdf_to_base64_images(pdf_bytes: bytes) -> list[str]:
    """Convert every PDF page to a high-res base64 PNG (2.5× zoom for sharper OCR)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5))
        images.append(base64.b64encode(pix.tobytes("png")).decode("utf-8"))
    doc.close()
    return images


def _header_crop_b64(pdf_bytes: bytes) -> str:
    """
    Return a base64 PNG of the top _HEADER_CROP_PCT of the first page at 2.5× zoom.

    The MV-104AN header row contains (left→right):
      Accident Date (Month/Day/Year) · No. of Vehicles · No. Injured · No. Killed
    Isolating this strip gives GPT-4o a large, unambiguous view of these adjacent
    fields so it cannot confuse 'No. of Vehicles = 1' with 'No. Injured'.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    rect = page.rect
    clip = fitz.Rect(0, 0, rect.width, rect.height * _HEADER_CROP_PCT)
    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), clip=clip)
    doc.close()
    return base64.b64encode(pix.tobytes("png")).decode("utf-8")


def extract_fields_from_pdf(pdf_bytes: bytes, api_key: str) -> dict:
    """
    Extract structured fields from a police report PDF using GPT-4o.

    Content sent to the model (in order):
      1. Extraction prompt (instructions)
      2. Reference values block (plate candidates + formatted dates from embedded text)
      3. Labelled header crop — top 15% of page 1, for accident date + injury counts
      4. Full-page images of all pages — for parties, vehicles, description, etc.
    """
    client = OpenAI(api_key=api_key)

    images = pdf_to_base64_images(pdf_bytes)

    # Pre-parse embedded text → only plates + dates (no raw numbers that could confuse counts)
    embedded_text = pdf_to_text(pdf_bytes)
    plate_candidates = _extract_plate_candidates(embedded_text)
    date_candidates = _extract_date_candidates(embedded_text)

    content = [{"type": "text", "text": EXTRACTION_PROMPT}]

    # Reference values block
    # Only dates from embedded text — plates are read purely from the image.
    # Embedded-text OCR on scanned forms is unreliable for plates (character substitutions
    # like 2→Z, 8→E); sending it as a hint causes the model to blend two wrong readings.
    reference_lines = []
    if date_candidates:
        reference_lines.append(f"Dates found in embedded text: {', '.join(date_candidates)}")

    if reference_lines:
        content.append({
            "type": "text",
            "text": (
                "\n\n--- REFERENCE VALUES ---\n"
                "Dates: use to cross-check the date you read from the header crop.\n"
                "Do NOT use these values for No. Injured or any other field.\n"
                + "\n".join(reference_lines)
                + "\n---"
            ),
        })

    # Header crop — labelled so the model knows exactly what it's looking at
    content.append({
        "type": "text",
        "text": (
            "\n\n--- ZOOMED HEADER CROP (top 20% of page 1) ---\n"
            "The very top of this image may contain a court filing stamp (e.g. NYSCEF) — ignore it.\n"
            "Below the stamp is the form header with the accident date boxes and three adjacent count fields:\n"
            "  [No. of Vehicles] [No. Injured] [No. Killed]\n"
            "Read each value from its own labelled box. Use this for date_of_accident "
            "and number_of_injured.\n---"
        ),
    })
    content.append({
        "type": "image_url",
        "image_url": {
            "url": f"data:image/png;base64,{_header_crop_b64(pdf_bytes)}",
            "detail": "high",
        },
    })

    # Full page images (all pages, for complete context)
    content.append({
        "type": "text",
        "text": "\n\n--- FULL PAGE IMAGES (all pages) ---",
    })
    for img_b64 in images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{img_b64}",
                "detail": "high",
            },
        })

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": content}],
        max_tokens=2000,
        temperature=0,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if the model wraps the JSON
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.startswith("```")).strip()

    return json.loads(raw)
