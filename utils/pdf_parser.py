import base64
import json
import re
import fitz  # PyMuPDF
from openai import OpenAI

EXTRACTION_PROMPT = """You are a legal data extraction specialist for police accident reports.

Your task is to carefully read the FULL document (including all pages) and extract exact values.

Pay special attention to:
- DATES: Look for fields labelled "Date of Accident", "Date/Time", "Crash Date". Return in MM/DD/YYYY format.
- LICENSE PLATES: Look in the "Vehicle" or "Registration" section for each driver.
  If a plate number appears in the REFERENCE VALUES below, use that value — it comes from the PDF's
  embedded text layer and is character-perfect. Only read the plate from the page image if no
  reference value is provided. Pay extra attention to letters vs numbers (e.g. O vs 0, I vs 1, S vs 5).
- DRIVER NAMES: Look in "Driver Information" or "Vehicle Operator" sections. Use the full legal name.
- NUMBER OF INJURED: This is CRITICAL. Police report forms contain a dedicated field for the total
  number of injured persons — typically labelled "No. Injured" (the words may be on separate lines).
  Read that field directly from the PAGE IMAGES and copy its value as an integer.
  Do NOT count individual injury checkboxes or severity codes — use only the summary count field.
  If the field is blank or absent, return 0.

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
- number_of_injured must be an integer read ONLY from the page images, never from reference values.
- Return ONLY the raw JSON object.
"""

# Tokens that look like plates but are common non-plate words
_PLATE_STOPWORDS = {
    'MV104AN', '104AN', 'VEHICLE', 'REPORT', 'POLICE', 'SECTOR', 'SEDAN',
    'COUPE', 'TRUCK', 'AVENUE', 'STREET', 'PATROL', 'REVIEW',
    'BICYCLIST', 'PEDESTRIAN', 'OTHER', 'AMENDED',
}


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
    raw = re.findall(r'\b\d{1,2}/\d{1,2}/(\d{4})\b', text)
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


def extract_fields_from_pdf(pdf_bytes: bytes, api_key: str) -> dict:
    """
    Extract structured fields from a police report PDF using GPT-4o.

    Strategy:
    - Page images are the primary (and only) source for ALL fields, including No. Injured.
    - Embedded text is pre-parsed to extract only plate candidates and formatted dates,
      which are sent as clean reference values. Raw embedded text is never sent — scanned
      police reports produce heavily garbled OCR that causes the model to misread count
      fields (No. Injured, No. Killed) when those garbled labels appear next to stray numbers.
    """
    client = OpenAI(api_key=api_key)

    images = pdf_to_base64_images(pdf_bytes)

    # Pre-parse embedded text → only plates + dates (no raw numbers that could confuse counts)
    embedded_text = pdf_to_text(pdf_bytes)
    plate_candidates = _extract_plate_candidates(embedded_text)
    date_candidates = _extract_date_candidates(embedded_text)

    content = [{"type": "text", "text": EXTRACTION_PROMPT}]

    reference_lines = []
    if plate_candidates:
        reference_lines.append(f"Plate numbers found in embedded text: {', '.join(plate_candidates)}")
    if date_candidates:
        reference_lines.append(f"Dates found in embedded text: {', '.join(date_candidates)}")

    if reference_lines:
        content.append({
            "type": "text",
            "text": (
                "\n\n--- REFERENCE VALUES ---\n"
                "Plate numbers: PREFER these over what you read from the images — "
                "they come from the PDF text layer and are character-perfect.\n"
                "Dates: use to cross-check the date you read from the images.\n"
                "Do NOT use these values for No. Injured or any other field.\n"
                + "\n".join(reference_lines)
                + "\n---"
            ),
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
