import base64
import json
import re
import fitz  # PyMuPDF
from openai import OpenAI

EXTRACTION_PROMPT = """You are a legal data extraction specialist for police accident reports.

Your task is to carefully read the FULL document (including all pages) and extract exact values.

You will receive:
  1. A ZOOMED HEADER CROP — top 20% of page 1. Use this for date_of_accident and
     number_of_injured. The header row contains three adjacent count fields:
       [No. of Vehicles] [No. Injured] [No. Killed]
     Read each value from its own labelled box — do NOT mix them up.
  2. A ZOOMED PLATE ROW CROP — a high-resolution strip of the vehicle registration row.
     Use this for vehicle_plate values. It shows both vehicles' plate numbers side-by-side.
  3. A ZOOMED DRIVER INFO CROP — the driver name / DOB / sex rows at 4× zoom.
     Use this for driver names (last, first, middle initial) and sex fields.
  4. A ZOOMED LOCATION CROP — the "Road on which accident occurred" section at 4× zoom.
     Use this for accident_location only (directional abbreviations are clearer at this zoom).
  5. FULL PAGE IMAGES — all pages at standard resolution for everything else
     (description, etc.).
  6. REFERENCE VALUES — formatted dates from the PDF text layer for cross-checking.

Pay special attention to:
- DATES: Read from the header crop (Month / Day / Year boxes). Return MM/DD/YYYY.
- LICENSE PLATES: Read from the PLATE ROW CROP — it is zoomed in for maximum clarity.
  The form uses a monospace typewriter-style font. Critical confusion pairs for this font:
    • 4 vs T — a typed 4 has a prominent crossbar that looks exactly like a capital T;
               if you see T at the start or within a plate, it is almost certainly the digit 4
    • X vs K — can look nearly identical in worn typewriter impressions
    • O vs 0, I vs 1, Z vs 2, B vs 8, S vs 5
  Copy the plate character by character from the plate row crop. Do not guess or infer.
- DRIVER NAMES: Read from the ZOOMED DRIVER INFO CROP for maximum accuracy.
  CRITICAL: Each vehicle has TWO name fields on the form:
    1. "Name exactly as it appears on REGISTRATION" — this is the REGISTERED OWNER. IGNORE this.
    2. "Name exactly as it appears on DRIVER LICENSE" (or "Operator") — this is the DRIVER. USE this.
  Always extract the DRIVER/OPERATOR name (driver license row), never the registered owner name.
  If the driver license name field is blank (owner is driving), then use the registration name.
  Names appear as LAST, FIRST, MIDDLE format. Middle initials are single characters — in
  typewriter font E and S are easily confused (same horizontal bar structure at small size).
  Read each letter of the name carefully; do not assume a middle initial based on context.
- SEX: Read the "Sex" box for each driver (typically a small M/F checkbox or filled box next
  to the driver's name/date-of-birth row). Return exactly "M" or "F". If not visible, return null.
- ACCIDENT LOCATION: Read from the ZOOMED LOCATION CROP — "Road on which accident occurred" line.
  NY police reports use directional abbreviations: N/B (Northbound), S/B (Southbound),
  E/B (Eastbound), W/B (Westbound). In typewriter font E and S look nearly identical —
  use the full location context (e.g. Long Island Expressway runs E/W, not N/S) to resolve ambiguity.
  The four possible values are N/B, S/B, E/B, W/B — never N, S, E, W alone.
- NUMBER OF INJURED: Read only from the "No. Injured" box in the HEADER CROP.
  NOT the "No. of Vehicles" box (immediately to its left), NOT "No. Killed" (to its right).

Return ONLY this JSON object (no markdown, no explanation):

{
    "parties": [
        {
            "name": "Operator/Driver name (from driver license row, NOT registered owner)",
            "role": "DRIVER 1, DRIVER 2, PEDESTRIAN, etc.",
            "vehicle_plate": "Exact plate number as printed, or null",
            "vehicle_description": "Year Make Model, or null",
            "sex": "M or F exactly as printed in the Sex box, or null"
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
- number_of_injured comes from the "No. Injured" box in the header crop only.
- Return ONLY the raw JSON object.
"""

# Tokens that look like plates but are common non-plate words
_PLATE_STOPWORDS = {
    'MV104AN', '104AN', 'VEHICLE', 'REPORT', 'POLICE', 'SECTOR', 'SEDAN',
    'COUPE', 'TRUCK', 'AVENUE', 'STREET', 'PATROL', 'REVIEW',
    'BICYCLIST', 'PEDESTRIAN', 'OTHER', 'AMENDED',
}

# Top fraction of page 1 containing the report header row
# (Accident Date · No. of Vehicles · No. Injured · No. Killed).
# 20% gives headroom for court filing stamps (NYSCEF etc.) at the very top.
_HEADER_CROP_Y0 = 0.00
_HEADER_CROP_Y1 = 0.20

# Vertical band containing the vehicle registration / plate number row.
# On MV-104AN/A forms the plate row sits at roughly 33–36% of the page.
# 30–40% gives a generous margin for scan/form-variant variation.
# Rendered at 5× zoom for maximum character-level clarity.
_PLATE_CROP_Y0   = 0.30
_PLATE_CROP_Y1   = 0.40
_PLATE_CROP_ZOOM = 5.0

# Vertical band containing the driver information rows (name, DOB, sex, insurance).
# On MV-104AN/A forms the "Driver Name as printed on license" row starts at ~18% of the page.
# Start at 15% to ensure the top operator-name row is fully captured (not clipped).
# The registration/owner name row follows immediately below it (~28-32%).
# Rendered at 4× zoom so middle initials and other small characters are unambiguous.
_DRIVER_CROP_Y0   = 0.15
_DRIVER_CROP_Y1   = 0.45
_DRIVER_CROP_ZOOM = 4.0

# Vertical band containing "Road on which accident occurred" (accident location).
# On MV-104AN/A forms this line sits at roughly 58–65% of the page.
# Rendered at 4× zoom so directional abbreviations (E/B, W/B, N/B, S/B) are unambiguous.
_LOCATION_CROP_Y0   = 0.54
_LOCATION_CROP_Y1   = 0.67
_LOCATION_CROP_ZOOM = 4.0


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


def _page_crop_b64(pdf_bytes: bytes, y0_pct: float, y1_pct: float, zoom: float) -> str:
    """
    Crop a horizontal band from page 1 and return it as a base64 PNG.
    y0_pct / y1_pct are fractions of the page height (0.0–1.0).
    zoom is the render scale factor.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    rect = page.rect
    clip = fitz.Rect(0, rect.height * y0_pct, rect.width, rect.height * y1_pct)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    doc.close()
    return base64.b64encode(pix.tobytes("png")).decode("utf-8")


def extract_fields_from_pdf(pdf_bytes: bytes, api_key: str) -> dict:
    """
    Extract structured fields from a police report PDF using GPT-4o.

    Content sent to the model (in order):
      1. Extraction prompt (instructions)
      2. Reference values (formatted dates from embedded text for cross-checking)
      3. Zoomed header crop   — top 20% at 2.5×, for date + injury counts
      4. Zoomed plate crop    — 30–40% at 5×, for vehicle plate numbers
      5. Zoomed driver crop   — 20–45% at 4×, for driver names + sex fields
      6. Zoomed location crop — 54–67% at 4×, for accident location / direction
      7. Full-page images     — all pages at 2.5× for everything else
    """
    client = OpenAI(api_key=api_key)

    images = pdf_to_base64_images(pdf_bytes)

    embedded_text = pdf_to_text(pdf_bytes)
    date_candidates = _extract_date_candidates(embedded_text)

    content = [{"type": "text", "text": EXTRACTION_PROMPT}]

    # Reference values: dates only (plates read purely from image)
    if date_candidates:
        content.append({
            "type": "text",
            "text": (
                "\n\n--- REFERENCE VALUES ---\n"
                "Dates: use to cross-check the date you read from the header crop.\n"
                "Do NOT use for No. Injured or any other field.\n"
                f"Dates found in embedded text: {', '.join(date_candidates)}\n---"
            ),
        })

    # 1 — Header crop (date + injury counts)
    content.append({
        "type": "text",
        "text": (
            "\n\n--- ZOOMED HEADER CROP (top 20% of page 1, 2.5× zoom) ---\n"
            "The very top may contain a court filing stamp (e.g. NYSCEF) — ignore it.\n"
            "Below the stamp: accident date boxes and [No. of Vehicles] [No. Injured] [No. Killed].\n"
            "Use this image for date_of_accident and number_of_injured.\n---"
        ),
    })
    content.append({
        "type": "image_url",
        "image_url": {
            "url": f"data:image/png;base64,{_page_crop_b64(pdf_bytes, _HEADER_CROP_Y0, _HEADER_CROP_Y1, 2.5)}",
            "detail": "high",
        },
    })

    # 2 — Plate row crop (vehicle registration row)
    content.append({
        "type": "text",
        "text": (
            "\n\n--- ZOOMED PLATE ROW CROP (30–40% of page 1, 5× zoom) ---\n"
            "This strip shows the 'Plate Number / State of Reg / Vehicle Year & Make' row "
            "for both vehicles. Use this image for vehicle_plate values.\n"
            "The font is monospace typewriter — remember: 4 looks like T, X looks like K.\n---"
        ),
    })
    content.append({
        "type": "image_url",
        "image_url": {
            "url": f"data:image/png;base64,{_page_crop_b64(pdf_bytes, _PLATE_CROP_Y0, _PLATE_CROP_Y1, _PLATE_CROP_ZOOM)}",
            "detail": "high",
        },
    })

    # 3 — Driver info crop (name / DOB / sex rows)
    content.append({
        "type": "text",
        "text": (
            "\n\n--- ZOOMED DRIVER INFO CROP (20–45% of page 1, 4× zoom) ---\n"
            "Each vehicle has TWO name rows: (1) Registered Owner and (2) Operator/Driver.\n"
            "ALWAYS use the OPERATOR/DRIVER name (driver license row) — NOT the registered owner.\n"
            "If the operator row is blank, fall back to the registered owner name.\n"
            "Names are in LAST, FIRST, MIDDLE format. Middle initials are single characters —\n"
            "in typewriter font E and S look nearly identical; read each character carefully.\n---"
        ),
    })
    content.append({
        "type": "image_url",
        "image_url": {
            "url": f"data:image/png;base64,{_page_crop_b64(pdf_bytes, _DRIVER_CROP_Y0, _DRIVER_CROP_Y1, _DRIVER_CROP_ZOOM)}",
            "detail": "high",
        },
    })

    # 4 — Location crop ("Road on which accident occurred" row)
    content.append({
        "type": "text",
        "text": (
            "\n\n--- ZOOMED LOCATION CROP (54–67% of page 1, 4× zoom) ---\n"
            "Find the 'Road on which accident occurred' line. Use it for accident_location.\n"
            "Watch for typewriter E vs S confusion in directional abbreviations:\n"
            "  E/B = Eastbound, W/B = Westbound, N/B = Northbound, S/B = Southbound.\n---"
        ),
    })
    content.append({
        "type": "image_url",
        "image_url": {
            "url": f"data:image/png;base64,{_page_crop_b64(pdf_bytes, _LOCATION_CROP_Y0, _LOCATION_CROP_Y1, _LOCATION_CROP_ZOOM)}",
            "detail": "high",
        },
    })

    # 6 — Full page images (all pages)
    content.append({
        "type": "text",
        "text": "\n\n--- FULL PAGE IMAGES (all pages, 2.5× zoom) ---",
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
