"""
Richards & Law — Automated Case Intake
Swans Applied AI Hackathon
"""

import time
import urllib.parse
from datetime import datetime

import requests
import streamlit as st

from utils.clio_client import ClioClient
from utils.email_utils import compose_client_email, get_scheduling_link, send_email
from utils.pdf_parser import extract_fields_from_pdf

# ── Environment ───────────────────────────────────────────────────────────────
CLIO_CLIENT_ID     = st.secrets["CLIO_CLIENT_ID"]
CLIO_CLIENT_SECRET = st.secrets["CLIO_CLIENT_SECRET"]
CLIO_REDIRECT_URI  = st.secrets.get("CLIO_REDIRECT_URI", "http://127.0.0.1:8502")
CLIO_AUTH_URL      = "https://eu.app.clio.com/oauth/authorize"
CLIO_TOKEN_URL     = "https://eu.app.clio.com/oauth/token"
OPENAI_API_KEY     = st.secrets["OPENAI_API_KEY"]
GMAIL_ADDRESS      = st.secrets["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = st.secrets["GMAIL_APP_PASSWORD"]

# ── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Richards & Law — Case Intake",
    page_icon="⚖️",
    layout="centered",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    .step-badge {
        background-color: #1a3c5e; color: white;
        padding: 4px 12px; border-radius: 20px;
        font-size: 13px; font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Session State Init ────────────────────────────────────────────────────────
DEFAULTS = {
    "access_token": None,
    "step": 1,
    "pdf_bytes": None,
    "client_email": None,
    "extracted_data": None,
    "contact": None,
    "matter": None,
    "selected_client_idx": None,
    "form_data": None,
    "custom_fields": None,
    "document_bytes": None,
    "document_filename": None,
    "email_subject": None,
    "email_body": None,
    "clio_updated": False,
    "sol_created": False,
    "retainer_generated": False,
    "email_sent": False,
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── OAuth Helpers ─────────────────────────────────────────────────────────────
def build_auth_url() -> str:
    params = {
        "response_type": "code",
        "client_id": CLIO_CLIENT_ID,
        "redirect_uri": CLIO_REDIRECT_URI,
    }
    return f"{CLIO_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> str:
    # Clio requires client credentials in the POST body (not Basic Auth)
    resp = requests.post(
        CLIO_TOKEN_URL,
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "client_id":     CLIO_CLIENT_ID,
            "client_secret": CLIO_CLIENT_SECRET,
            "redirect_uri":  CLIO_REDIRECT_URI,
        },
    )
    if not resp.ok:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"HTTP {resp.status_code} — {detail}")
    return resp.json()["access_token"]


# ── OAuth Callback Handling ───────────────────────────────────────────────────
query_params = st.query_params

# Surface any error Clio sends back (e.g. access_denied)
if "error" in query_params:
    st.error(f"⚠️ Clio returned an error: `{query_params.get('error')}` — {query_params.get('error_description', 'No details provided.')}")
    if st.button("Try connecting again"):
        st.query_params.clear()
        st.rerun()
    st.stop()

if "code" in query_params and not st.session_state.access_token:
    code = query_params.get("code")
    with st.spinner("Completing Clio authentication…"):
        try:
            token = exchange_code(code)
            st.session_state.access_token = token
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"⚠️ Clio token exchange failed: {e}")
            st.info(
                "Common causes: \n"
                "- The redirect URI in your Clio Developer App doesn't match exactly: "
                f"`{CLIO_REDIRECT_URI}`\n"
                "- The authorization code expired (try connecting again)"
            )
            if st.button("Try connecting again"):
                st.query_params.clear()
                st.rerun()
            st.stop()


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image(
        "https://framerusercontent.com/images/WyNMWWZTqmGfCzT7ULicrHbsA.png",
        width=120,
    )
    st.markdown("### ⚖️ Richards & Law")
    st.markdown("**Automated Case Intake**")
    st.divider()

    STEP_LABELS = [
        "1 · Upload Report",
        "2 · Select Client",
        "3 · Review Data",
        "4 · Update Clio",
        "5 · Retainer",
        "6 · Send Email",
    ]
    for i, label in enumerate(STEP_LABELS, start=1):
        if i < st.session_state.step:
            st.markdown(f"✅ ~~{label}~~")
        elif i == st.session_state.step:
            st.markdown(f"**▶ {label}**")
        else:
            st.markdown(f"○ {label}")

    st.divider()
    if st.session_state.access_token:
        st.success("Clio: Connected")
    else:
        st.warning("Clio: Not connected")


# ── Gate: Require Clio Auth ───────────────────────────────────────────────────
if not st.session_state.access_token:
    st.title("⚖️ Richards & Law — Case Intake")
    st.markdown("Connect your Clio account to begin processing a new police report.")
    st.markdown(
        f'<a href="{build_auth_url()}" target="_self">'
        f'<button style="background:#1a3c5e;color:white;padding:12px 24px;'
        f'border:none;border-radius:6px;font-size:15px;cursor:pointer;">'
        f'🔗 Connect to Clio Manage</button></a>',
        unsafe_allow_html=True,
    )
    st.stop()

clio = ClioClient(st.session_state.access_token)

# ── Progress Bar ──────────────────────────────────────────────────────────────
progress = (st.session_state.step - 1) / 5
st.progress(progress, text=f"Step {st.session_state.step} of 6")
st.divider()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Upload PDF & Enter Client Email
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state.step == 1:
    st.markdown('<span class="step-badge">STEP 1</span>', unsafe_allow_html=True)
    st.header("Upload Police Report")
    st.caption("Upload the scanned police accident report and enter the client's email to locate their Matter in Clio.")

    uploaded_file = st.file_uploader("Police Accident Report (PDF)", type=["pdf"])
    client_email  = st.text_input("Client Email Address", placeholder="client@example.com")

    if st.button("Continue →", type="primary", use_container_width=True):
        if not uploaded_file:
            st.error("Please upload a PDF file.")
            st.stop()
        if not client_email:
            st.error("Please enter the client's email address.")
            st.stop()

        with st.spinner("Looking up client in Clio…"):
            contact = clio.get_contact_by_email(client_email)
            if not contact:
                st.error(f"No contact found in Clio for **{client_email}**.")
                with st.expander("🔍 Debug — contacts found in Clio"):
                    try:
                        sample = clio.debug_list_contacts(limit=10)
                        if sample:
                            for c in sample:
                                st.write(f"**{c['name']}**")
                                st.write(f"  → email_addresses raw: `{c.get('email_addresses')}`")
                                st.write(f"  → primary_email_address: `{c.get('primary_email_address')}`")
                        else:
                            st.write("No contacts found in this Clio account at all.")
                    except Exception as dbg_err:
                        st.write(f"Debug fetch failed: {dbg_err}")
                st.stop()

        with st.spinner("Fetching linked matter…"):
            matters = clio.get_matters_for_contact(contact["id"])
            if not matters:
                st.error(f"No matters found for **{contact['name']}**.")
                st.stop()

        with st.spinner("Extracting data from police report with AI (this may take ~15s)…"):
            try:
                extracted = extract_fields_from_pdf(uploaded_file.read(), OPENAI_API_KEY)
            except Exception as e:
                st.error(f"PDF extraction failed: {e}")
                st.stop()

        st.session_state.contact        = contact
        st.session_state.matter         = matters[0]
        st.session_state.client_email   = client_email
        st.session_state.extracted_data = extracted
        st.session_state.step           = 2
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Select Which Party Is Our Client
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.step == 2:
    st.markdown('<span class="step-badge">STEP 2</span>', unsafe_allow_html=True)
    st.header("Select the Client")

    data    = st.session_state.extracted_data
    parties = data.get("parties", [])
    contact = st.session_state.contact

    st.info(f"**Clio Contact:** {contact['name']}  |  **Matter:** {st.session_state.matter.get('display_number', 'N/A')}")

    if not parties:
        st.error("No parties were found in the report. Please go back and check the PDF.")
        if st.button("← Back"):
            st.session_state.step = 1
            st.rerun()
        st.stop()

    options = [
        f"{p.get('name', 'Unknown')}  —  {p.get('role', '')}  —  Plate: {p.get('vehicle_plate') or 'N/A'}"
        for p in parties
    ]
    selected_label = st.radio("Which party is **our client**?", options)
    selected_idx   = options.index(selected_label)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Back", use_container_width=True):
            st.session_state.step = 1
            st.rerun()
    with col2:
        if st.button("Confirm →", type="primary", use_container_width=True):
            st.session_state.selected_client_idx = selected_idx
            st.session_state.step = 3
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Review & Edit Extracted Data
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.step == 3:
    st.markdown('<span class="step-badge">STEP 3</span>', unsafe_allow_html=True)
    st.header("Review Extracted Data")
    st.caption("Verify and correct the AI-extracted fields before pushing to Clio.")

    data           = st.session_state.extracted_data
    parties        = data.get("parties", [])
    client_party   = parties[st.session_state.selected_client_idx]
    defendant_party = next(
        (p for i, p in enumerate(parties) if i != st.session_state.selected_client_idx),
        {},
    )

    with st.form("review_form"):
        st.subheader("Parties")
        col1, col2 = st.columns(2)
        with col1:
            client_name   = st.text_input("Client Name *",   value=client_party.get("name", ""))
            pronoun       = st.selectbox("Client Pronoun *", ["his", "her", "their"])
            plate_number  = st.text_input("Client Vehicle Plate", value=client_party.get("vehicle_plate") or "")
        with col2:
            defendant_name = st.text_input("Defendant Name *", value=defendant_party.get("name", ""))

        st.subheader("Accident Details")
        col3, col4 = st.columns(2)
        with col3:
            date_of_accident  = st.text_input("Date of Accident * (MM/DD/YYYY)", value=data.get("date_of_accident", ""))
            accident_location = st.text_input("Accident Location *", value=data.get("accident_location", ""))
        with col4:
            num_injured = st.number_input(
                "Number of Injured *",
                min_value=0,
                value=int(data.get("number_of_injured") or 0),
            )

        accident_description = st.text_area(
            "Accident Description *",
            value=data.get("accident_description", ""),
            height=100,
        )

        col5, col6 = st.columns(2)
        with col5:
            back    = st.form_submit_button("← Back", use_container_width=True)
        with col6:
            approve = st.form_submit_button("✅ Approve & Continue", type="primary", use_container_width=True)

        if back:
            st.session_state.step = 2
            st.rerun()

        if approve:
            # Basic validation
            missing = [
                f for f, v in {
                    "Client Name": client_name,
                    "Defendant Name": defendant_name,
                    "Date of Accident": date_of_accident,
                    "Accident Location": accident_location,
                    "Accident Description": accident_description,
                }.items() if not v.strip()
            ]
            if missing:
                st.error(f"Please fill in: {', '.join(missing)}")
            else:
                st.session_state.form_data = {
                    "client_name":           client_name.strip().title(),
                    "defendant_name":         defendant_name.strip().title(),
                    "pronoun":               pronoun,
                    "date_of_accident":      date_of_accident.strip(),
                    "accident_location":     accident_location.strip(),
                    "plate_number":          plate_number.strip(),
                    "num_injured":           num_injured,
                    "accident_description":  accident_description.strip(),
                }
                st.session_state.step = 4
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Update Clio Matter + Create SoL Calendar Event
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.step == 4:
    st.markdown('<span class="step-badge">STEP 4</span>', unsafe_allow_html=True)
    st.header("Update Clio Matter")

    form_data = st.session_state.form_data
    matter    = st.session_state.matter

    # ── Hardcoded custom field IDs (account-level, never change) ─────────────
    # field name → Clio custom_field.id
    CF_IDS = {
        "Date of Accident":     483062,
        "Defendant Name":       483068,
        "Accident Location":    483071,
        "Client Vehicle Plate": 483074,
        "Number of Injured":    483077,
        "Pronoun":              483080,
        "Accident Description": 483065,
        "Pronoun2":             483152,   # auto-derived: he / she / they
        "CustomParagraph":      483155,   # auto-derived: injury vs property-only clause
    }

    # type prefix used in cfv stub IDs → sorted list of CF IDs with that type
    # (sorted ascending = creation order, matching cfv stub numeric suffix order)
    CF_BY_PREFIX = {
        "date":      [483062],
        "text_area": [483065, 483155],                          # Accident Description, CustomParagraph
        "text_line": [483068, 483071, 483074, 483080, 483152],  # 4 original + Pronoun2
        "numeric":   [483077],
    }

    # ── Build payload ─────────────────────────────────────────────────────────
    try:
        accident_dt_iso = datetime.strptime(
            form_data["date_of_accident"], "%m/%d/%Y"
        ).strftime("%Y-%m-%d")
    except ValueError:
        accident_dt_iso = form_data["date_of_accident"]  # already ISO

    # Pronoun2: subject form derived from possessive pronoun
    pronoun2 = {"his": "he", "her": "she", "their": "they"}.get(
        form_data["pronoun"], form_data["pronoun"]
    )

    # CustomParagraph: injury clause vs property-only clause
    if int(form_data["num_injured"]) > 0:
        custom_paragraph = (
            "Additionally, since the motor vehicle accident involved an injured person, "
            "Attorney will also investigate potential bodily injury claims and review "
            "relevant medical records to substantiate non-economic damages."
        )
    else:
        custom_paragraph = (
            "However, since the motor vehicle accident involved no reported injured "
            "people, the scope of this engagement is strictly limited to the recovery "
            "of property damage and loss of use."
        )

    field_values_map = {
        "Date of Accident":     accident_dt_iso,
        "Defendant Name":       form_data["defendant_name"],
        "Accident Location":    form_data["accident_location"],
        "Client Vehicle Plate": form_data["plate_number"],
        "Number of Injured":    int(form_data["num_injured"]),
        "Pronoun":              form_data["pronoun"],
        "Accident Description": form_data["accident_description"],
        "Pronoun2":             pronoun2,
        "CustomParagraph":      custom_paragraph,
    }

    # ── Resolve existing cfv composite IDs from matter stubs ─────────────────
    # Clio returns cfv stubs like {"id": "text_line-109328069"} with no individual
    # GET endpoint. We match them to CF IDs by type prefix + sorted numeric order
    # (both sequences are in creation order, so zip is correct).
    from collections import defaultdict

    with st.spinner("Checking existing matter field values…"):
        fresh_matter = clio.get_matter_by_id(matter["id"])
        cfv_stubs    = fresh_matter.get("custom_field_values") or []

        stubs_by_prefix: dict = defaultdict(list)
        for stub in cfv_stubs:
            cid = str(stub.get("id", ""))
            if "-" in cid:
                prefix = cid.rsplit("-", 1)[0]
                stubs_by_prefix[prefix].append(cid)
        for prefix in stubs_by_prefix:
            stubs_by_prefix[prefix].sort(key=lambda s: int(s.rsplit("-", 1)[-1]))

        # cf_id → composite cfv stub id (only for fields that already have a value)
        cfv_id_map: dict = {}
        for prefix, cf_ids in CF_BY_PREFIX.items():
            for cf_id, composite_id in zip(cf_ids, stubs_by_prefix.get(prefix, [])):
                cfv_id_map[cf_id] = composite_id

    custom_field_values = []
    for name, value in field_values_map.items():
        cf_id = CF_IDS.get(name)
        if cf_id is None:
            continue
        entry = {"custom_field": {"id": cf_id}, "value": value}
        if cf_id in cfv_id_map:
            entry["id"] = cfv_id_map[cf_id]   # UPDATE existing value
        custom_field_values.append(entry)

    # ── Update Matter ─────────────────────────────────────────────────────────
    if not st.session_state.clio_updated:
        with st.spinner("Updating matter in Clio…"):
            try:
                clio.update_matter(matter["id"], custom_field_values)
                st.session_state.clio_updated = True
            except Exception as e:
                st.error(f"Failed to update matter: {e}")
                st.stop()

    st.success("✅ Matter updated in Clio with all case fields.")

    # ── Create SoL Calendar Event ─────────────────────────────────────────────
    if not st.session_state.sol_created:
        with st.spinner("Creating Statute of Limitations calendar event…"):
            try:
                accident_dt = datetime.strptime(form_data["date_of_accident"], "%m/%d/%Y")
                sol_dt      = accident_dt.replace(year=accident_dt.year + 8)
                sol_iso     = sol_dt.strftime("%Y-%m-%dT09:00:00Z")

                clio.create_calendar_event(
                    matter_id        = matter["id"],
                    summary          = f"⚠️ Statute of Limitations — {form_data['client_name']}",
                    description      = (
                        f"SOL deadline for {form_data['client_name']}. "
                        f"Accident date: {form_data['date_of_accident']}. "
                        f"This event was auto-generated by the Case Intake system."
                    ),
                    event_date_iso   = sol_iso,
                )
                st.session_state.sol_created = True
            except Exception as e:
                st.warning(f"Calendar event could not be created: {e}")

    if st.session_state.sol_created:
        accident_dt = datetime.strptime(form_data["date_of_accident"], "%m/%d/%Y")
        sol_date    = accident_dt.replace(year=accident_dt.year + 8).strftime("%B %d, %Y")
        st.success(f"✅ Statute of Limitations calendared for **{sol_date}** (8 years from accident).")

    st.divider()
    if st.button("Continue to Retainer →", type="primary", use_container_width=True):
        st.session_state.step = 5
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Generate Retainer Agreement via Clio Document Automation
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.step == 5:
    st.markdown('<span class="step-badge">STEP 5</span>', unsafe_allow_html=True)
    st.header("Retainer Agreement")
    st.caption("Generate the retainer agreement using Clio's document automation, then approve to continue.")

    matter = st.session_state.matter

    # Hardcoded retainer template ID (account-level, never changes)
    RETAINER_TEMPLATE_ID = 359702

    # ── Generate ──────────────────────────────────────────────────────────────
    if not st.session_state.retainer_generated:
        if st.button("🔄 Generate Retainer Agreement", type="primary", use_container_width=True):
            with st.spinner("Triggering Clio document automation…"):
                try:
                    clio.generate_document(RETAINER_TEMPLATE_ID, matter["id"])
                    time.sleep(4)  # Allow Clio time to process
                    st.session_state.retainer_generated = True
                except Exception as e:
                    st.error(f"Document generation failed: {e}")
                    st.stop()
            st.rerun()

    if st.session_state.retainer_generated:
        st.success("✅ Retainer agreement generated and stored in Clio.")

        # ── Fetch & cache document bytes ──────────────────────────────────────
        if not st.session_state.document_bytes:
            with st.spinner("Downloading document…"):
                try:
                    docs = clio.get_documents_for_matter(matter["id"])
                    if docs:
                        latest = docs[0]  # already sorted desc by created_at
                        st.session_state.document_bytes    = clio.download_document(latest["id"])
                        st.session_state.document_filename = latest["name"]
                except Exception as e:
                    st.warning(f"Could not download document for preview: {e}")

        if st.session_state.document_bytes:
            st.download_button(
                "📄 Preview / Download Retainer",
                data=st.session_state.document_bytes,
                file_name=st.session_state.document_filename or "retainer_agreement.pdf",
                mime="application/pdf",
                use_container_width=True,
            )

        st.divider()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← Back", use_container_width=True):
                st.session_state.step = 4
                st.rerun()
        with col2:
            if st.button("✅ Approve & Continue to Email →", type="primary", use_container_width=True):
                st.session_state.step = 6
                st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 6 — Draft & Send Client Email
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.step == 6:
    st.markdown('<span class="step-badge">STEP 6</span>', unsafe_allow_html=True)
    st.header("Client Email")
    st.caption("Review the personalized email draft, then approve to send.")

    form_data = st.session_state.form_data
    contact   = st.session_state.contact

    # Resolve client email from Clio contact
    # Clio returns email_addresses as stubs (no "address" field);
    # the actual email is in the top-level primary_email_address string.
    to_email = (
        contact.get("primary_email_address")
        or st.session_state.client_email
    )

    # Build email
    scheduling_link, season_label = get_scheduling_link()
    subject, body = compose_client_email(
        client_name          = form_data["client_name"],
        defendant_name       = form_data["defendant_name"],
        date_of_accident     = form_data["date_of_accident"],
        accident_description = form_data["accident_description"],
    )

    # Display preview (editable)
    st.subheader("Email Preview")
    col_a, col_b = st.columns([1, 2])
    with col_a:
        st.markdown(f"**To:** `{to_email}`")
        st.markdown(f"**Booking:** {season_label.title()} link")
    with col_b:
        if st.session_state.document_bytes:
            st.success(f"📎 Retainer PDF attached: `{st.session_state.document_filename}`")
        else:
            st.warning("No retainer PDF attached (generate it in Step 5 first).")

    edited_subject = st.text_input("Subject", value=subject)
    edited_body    = st.text_area("Email Body (HTML)", value=body, height=340)

    st.divider()

    if st.session_state.email_sent:
        st.success("✅ Email already sent for this session.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("← Back", use_container_width=True):
                st.session_state.step = 5
                st.rerun()
        with col2:
            if st.button("📤 Approve & Send Email", type="primary", use_container_width=True):
                with st.spinner("Sending email…"):
                    try:
                        send_email(
                            gmail_address      = GMAIL_ADDRESS,
                            gmail_password     = GMAIL_APP_PASSWORD,
                            to_email           = to_email,
                            subject            = edited_subject,
                            body_html          = edited_body,
                            attachment_bytes   = st.session_state.document_bytes,
                            attachment_filename = st.session_state.document_filename or "retainer_agreement.pdf",
                        )
                        st.session_state.email_sent = True
                    except Exception as e:
                        st.error(f"Failed to send email: {e}")
                        st.stop()
                st.rerun()

    # ── Completion Summary ────────────────────────────────────────────────────
    if st.session_state.email_sent:
        st.balloons()
        st.divider()
        st.subheader("🎉 Case Intake Complete!")

        accident_dt = datetime.strptime(form_data["date_of_accident"], "%m/%d/%Y")
        sol_date    = accident_dt.replace(year=accident_dt.year + 8).strftime("%B %d, %Y")

        st.markdown(f"""
| Step | Status |
|------|--------|
| Police report parsed by AI | ✅ |
| Client matched in Clio | ✅ `{contact['name']}` |
| Matter updated with case fields | ✅ `{st.session_state.matter.get('display_number', '')}` |
| Statute of Limitations calendared | ✅ `{sol_date}` |
| Retainer agreement generated in Clio | ✅ |
| Personalized email sent to client | ✅ `{to_email}` |
        """)

        if st.button("🔄 Start New Case", use_container_width=True):
            for k, v in DEFAULTS.items():
                st.session_state[k] = v
            st.session_state.access_token = clio.access_token
            st.rerun()
