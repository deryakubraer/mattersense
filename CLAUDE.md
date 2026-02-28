# CLAUDE.md — Richards & Law Automated Case Intake

> Swans Applied AI Hackathon · Legal Engineer track
> A Streamlit app that automates the entire new-client intake pipeline for a NY personal injury law firm, from police report upload to retainer delivery.

---

## Goal

When a new accident victim calls Richards & Law, a paralegal currently spends hours manually:
- Reading a police report PDF
- Entering data into Clio Manage
- Drafting a retainer agreement
- Writing and sending a welcome email

This app collapses all of that into **one 6-step wizard** that takes under 5 minutes.

---

## Architecture

```
swan_hackathon/
├── app.py                      # Main Streamlit app (6-step wizard)
├── utils/
│   ├── clio_client.py          # Clio Manage API v4 wrapper (EU region)
│   ├── pdf_parser.py           # GPT-4o PDF extraction (vision + embedded text)
│   └── email_utils.py          # Gmail SMTP email composition + sending
├── .env                        # Credentials (never commit)
├── .streamlit/config.toml      # Forces port 8502
├── .claude/launch.json         # preview_start config
└── requirements.txt
```

### Environment

- **Conda env**: `swan_hackathon` (Python 3.11)
- **Run**: `conda run -n swan_hackathon streamlit run app.py`
- **Or via preview tool**: server name `swan_hackathon` in `.claude/launch.json`
- **Port**: 8502 (hardcoded in `.streamlit/config.toml`)
- **Open in browser**: `http://127.0.0.1:8502` — do NOT use the preview tool for OAuth (it blocks external redirects)

### Dependencies

```
streamlit>=1.32.0
openai>=1.0.0
requests>=2.31.0
python-dotenv>=1.0.0
PyMuPDF>=1.23.0
```

---

## The 6-Step Workflow

### Step 1 — Upload Police Report
- Paralegal uploads a police accident report PDF and enters the client's email
- App looks up the **Clio contact** by email (3-strategy search: full email query → local-part query → paginate all)
- Fetches the contact's first linked **Clio matter**
- Runs **GPT-4o extraction** on the PDF (vision + embedded text, 2.5× zoom)
- Extracted fields: parties, date_of_accident, accident_location, accident_description, number_of_injured, report_number

### Step 2 — Select Client
- Shows all parties extracted from the report (name, role, plate)
- Paralegal selects which party is **our client** (the plaintiff)
- The other party becomes the defendant

### Step 3 — Review Extracted Data
- Editable form pre-filled with AI-extracted values
- Fields: client name, pronoun (his/her/their), vehicle plate, defendant name, date, location, number injured, description
- Paralegal corrects any OCR/AI errors before pushing to Clio

### Step 4 — Update Clio Matter + Create SoL Calendar Event
- Ensures 7 custom fields exist on the Clio Matter (creates them if missing)
- Updates those custom fields on the matter with the reviewed data
- Creates a **Statute of Limitations** calendar event: accident date + 8 years (NY personal injury SoL)

**Custom fields pushed to Clio:**

| Field Name           | Clio Type  |
|----------------------|------------|
| Date of Accident     | Date       |
| Defendant Name       | TextLine   |
| Accident Location    | TextLine   |
| Client Vehicle Plate | TextLine   |
| Number of Injured    | Numeric    |
| Pronoun              | TextLine   |
| Accident Description | TextArea   |

### Step 5 — Generate Retainer Agreement
- Lists Clio document automation templates
- Triggers generation of a retainer PDF via Clio's document automation API
- Waits 4 seconds for Clio to process, then fetches the generated document
- Paralegal previews/downloads before approving

### Step 6 — Draft & Send Client Email
- Composes a personalized HTML email with:
  - Client's first name, accident date, accident description
  - Seasonal Calendly booking link (March–August = in-office, September–February = virtual)
  - Retainer PDF attached
- Paralegal reviews and edits subject + body before sending via Gmail SMTP

---

## Credentials (.env)

```
CLIO_CLIENT_ID=...
CLIO_CLIENT_SECRET=...
CLIO_REDIRECT_URI=http://127.0.0.1:8502
OPENAI_API_KEY=...
GMAIL_ADDRESS=deryakubraer@gmail.com
GMAIL_APP_PASSWORD=...         # Gmail App Password (not account password)
```

Calendly links are hardcoded in `utils/email_utils.py`:
- In-office (March–August): `https://calendly.com/swans-santiago-p/summer-spring`
- Virtual (September–February): `https://calendly.com/swans-santiago-p/winter-autumn`

---

## Clio API — Critical Knowledge

### Region
- This account is on the **EU region**: all API calls use `https://eu.app.clio.com`
- Auth URLs: `https://eu.app.clio.com/oauth/authorize` and `/oauth/token`
- API base: `https://eu.app.clio.com/api/v4`
- Using `app.clio.com` (US) causes a 401 `invalid_client` on token exchange

### OAuth Flow
- Authorization Code flow; redirect URI must exactly match the app registered in Clio Developer Portal
- Token exchange requires `client_id` + `client_secret` in the **POST body** (not Basic Auth header)
- After redirect, `st.query_params` captures the `?code=...` param and exchanges it

### Contact Email Field
- Clio returns email in `primary_email_address` (a top-level string), **not** `email_addresses[].address`
- `email_addresses` is returned as shallow stubs: `[{"id": 12789767, "etag": "..."}]` — no `address` field
- The `get_contact_by_email` method checks `primary_email_address` first, then falls back to the stubs array

### Custom Fields
- `field_type` enum must be exact CamelCase: `TextLine`, `TextArea`, `Date`, `Numeric`, `Checkbox`, `Contact`, `Currency`, `Time`, `Email`, `Matter`, `Picklist`, `Url`
- Lowercase variants (`text_field`, `date`, etc.) cause 422
- Filter param is `parent_type` (not `field_type`): `GET /custom_fields.json?parent_type=Matter`

### Custom Field Value Updates (the hard part)

This is the most complex part of the integration. When updating a matter's custom field values via `PATCH /matters/{id}.json`, Clio requires:

- **First time (no existing value)**: omit `id` in the payload entry → Clio creates a new cfv
- **Subsequent times (value already exists)**: include `"id": "<composite_cfv_id>"` → Clio updates it
- Omitting the `id` when a value already exists causes `422: custom field value for custom field {cf_id} already exists`

**How cfv IDs work:**
- `GET /matters/{id}.json?fields=custom_field_values` returns stubs like: `{"id": "text_line-109328069", "etag": "..."}`
- These composite string IDs (format: `{type_prefix}-{numeric_id}`) are **Clio's polymorphic identifiers**
- There is **no individual GET endpoint** for custom field values (`/custom_field_values/{id}.json` returns 404)
- The composite string (`"text_line-109328069"`) is what must go in the PATCH payload `id` field

**Matching stubs to custom fields (since we can't GET individual cfvs):**
- The type prefix (`date`, `text_area`, `text_line`, `numeric`) maps to the Clio `field_type` enum
- Stubs are sorted by their numeric suffix (= creation order)
- Our custom fields are sorted by Clio CF id (= creation order)
- We pair them up by type + sorted position → `cfv_id_map: {cf_id: composite_id}`

**Code location**: Step 4 in `app.py`, `_STUB_TYPE` dict + `cfv_id_map` builder

### Calendar Events
- Endpoint: `POST /calendar_entries.json`
- Payload: `{"summary", "description", "start_at", "end_at", "all_day": true, "matter": {"id": matter_id}}`

### Document Automation
- Endpoint: `POST /document_automation/matters/{matter_id}/documents`
- Requires a pre-existing template in Clio (created in the Clio UI)
- After triggering, wait ~4 seconds before fetching the generated document
- Fetch generated docs: `GET /documents.json?matter_id=...&order=created_at(desc)`

---

## PDF Extraction Strategy

`utils/pdf_parser.py` uses a dual-signal approach for maximum accuracy:

1. **Embedded text** via PyMuPDF `page.get_text("text")` — fast, exact for text-based PDFs
2. **Vision images** via PyMuPDF `page.get_pixmap(matrix=Matrix(2.5, 2.5))` — 2.5× zoom for sharpness

Both are sent together to **GPT-4o** with `temperature=0`. The embedded text helps GPT-4o verify dates and plate numbers that OCR might misread. The prompt explicitly instructs the model to copy dates and plates character-for-character.

---

## Email Logic

`utils/email_utils.py`:
- **Seasonal link selection**: `date.today().month` — months 3–8 → in-office, 9–2 → virtual
- **Transport**: Gmail SMTP SSL on port 465, using an App Password (not the account password)
- **HTML email** with embedded Calendly button and optional PDF attachment

---

## Known Issues / Current Status

### ✅ Working
- Clio OAuth (EU region)
- Contact lookup by email
- PDF extraction with GPT-4o
- Custom field creation
- Calendar event creation
- Seasonal email + Calendly links

### 🔄 In Progress
- **Step 4 matter update** — the cfv composite ID matching approach is implemented but awaiting validation that Clio accepts the composite string as the PATCH payload `id` field

### 📋 Not Yet Tested
- Step 5: Clio document automation (requires a template to exist in Clio)
- Step 6: End-to-end email send with retainer attachment
- Full run with the hackathon test case: `GUILLERMO_REYES_v_LIONEL_FRANCOIS` police report

---

## Debug Helpers

**Step 4 debug expanders** (visible on failure):
- `🔍 Debug — cfv lookup`: shows cfv stubs from matter + cfv_id_map built
- `🔍 Debug — PATCH payload`: shows the exact JSON being sent to Clio

**Step 1 debug expander** (shows on contact-not-found):
- Lists contacts with their raw `email_addresses` and `primary_email_address` fields

**`clio_client.debug_list_contacts(limit)`**: returns first N contacts with email fields for diagnostics.

---

## Session State Keys

| Key                  | Type    | Description                                      |
|----------------------|---------|--------------------------------------------------|
| `access_token`       | str     | Clio OAuth bearer token                          |
| `step`               | int     | Current wizard step (1–6)                        |
| `pdf_bytes`          | bytes   | Uploaded PDF raw bytes                           |
| `client_email`       | str     | Email entered at Step 1                          |
| `extracted_data`     | dict    | GPT-4o extraction result                         |
| `contact`            | dict    | Clio contact record                              |
| `matter`             | dict    | Clio matter record                               |
| `selected_client_idx`| int     | Index into `extracted_data["parties"]`           |
| `form_data`          | dict    | Reviewed form values from Step 3                 |
| `custom_fields`      | dict    | `{field_name: cf_id}` map                        |
| `document_bytes`     | bytes   | Generated retainer PDF bytes                     |
| `document_filename`  | str     | Filename of generated retainer                   |
| `clio_updated`       | bool    | Guard: matter update already completed           |
| `sol_created`        | bool    | Guard: SoL calendar event already created        |
| `retainer_generated` | bool    | Guard: document automation already triggered     |
| `email_sent`         | bool    | Guard: email already sent this session           |
