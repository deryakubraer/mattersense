import base64
import json
import fitz  # PyMuPDF
from openai import OpenAI

EXTRACTION_PROMPT = """You are a legal data extraction specialist for police accident reports.

Your task is to carefully read the FULL document (including all pages) and extract exact values.

Pay special attention to:
- DATES: Look for fields labelled "Date of Accident", "Date/Time", "Crash Date". Return in MM/DD/YYYY format.
- LICENSE PLATES: Look in the "Vehicle" or "Registration" section for each driver. Copy the plate number EXACTLY as printed. Pay extra attention to letters vs numbers (e.g. O vs 0, I vs 1, S vs 5) and include any dashes or spaces if present.
- DRIVER NAMES: Look in "Driver Information" or "Vehicle Operator" sections. Use the full legal name.
- NUMBER OF INJURED: This is CRITICAL. Police report forms contain a dedicated field
  for the total number of injured persons — it is typically labelled exactly as "No. Injured". No and Injured may be on separate lines.
  Read that field directly and copy its value as an integer. That field is the source of truth.
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
- number_of_injured must be an integer. NEVER use the contents of embedded text for this — the model should read it directly from the PDF page images.
- Return ONLY the raw JSON object.
"""


def pdf_to_text(pdf_bytes: bytes) -> str:
    """Extract embedded text from PDF using PyMuPDF (fast, works on text-based PDFs)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    doc.close()
    return "\n\n".join(pages).strip()


def pdf_to_base64_images(pdf_bytes: bytes) -> list[str]:
    """Convert every PDF page to a high-res base64 PNG (for scanned/image PDFs)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5))  # 2.5× for sharper OCR
        images.append(base64.b64encode(pix.tobytes("png")).decode("utf-8"))
    doc.close()
    return images


def extract_fields_from_pdf(pdf_bytes: bytes, api_key: str) -> dict:
    """
    Extract structured fields from a police report PDF using GPT-4o.
    Sends both embedded text (if any) AND page images for maximum accuracy.
    """
    client = OpenAI(api_key=api_key)

    # Always send images (works for both scanned and text PDFs)
    images = pdf_to_base64_images(pdf_bytes)

    # Also extract embedded text to give the model a second signal
    embedded_text = pdf_to_text(pdf_bytes)

    content = [{"type": "text", "text": EXTRACTION_PROMPT}]

    # Attach embedded text if available (helps with dates/plates that OCR may misread)
    if embedded_text:
        content.append({
            "type": "text",
            "text": f"\n\n--- EMBEDDED TEXT FROM PDF (use this to verify dates and plate numbers, but NEVER for No. Injured) ---\n{embedded_text[:6000]}\n---"
        })

    # Attach page images
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
        temperature=0,  # deterministic — no hallucination
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown code fences if the model wraps the JSON
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(line for line in lines if not line.startswith("```")).strip()

    return json.loads(raw)
