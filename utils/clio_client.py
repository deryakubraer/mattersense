import json
import logging
import requests

# Base URLs per Clio region — used for both OAuth and API calls.
CLIO_REGIONS: dict[str, str] = {
    "US": "https://app.clio.com",
    "EU": "https://eu.app.clio.com",
    "CA": "https://ca.app.clio.com",
    "AU": "https://au.app.clio.com",
}

# Configure a module-level logger — output goes to stderr / Streamlit server logs
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [CLIO] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clio_client")


class ClioClient:
    def __init__(self, access_token: str, region: str = "EU"):
        if region not in CLIO_REGIONS:
            raise ValueError(f"Unknown Clio region '{region}'. Valid: {list(CLIO_REGIONS)}")
        self.access_token = access_token
        self.region = region
        self.api_base = f"{CLIO_REGIONS[region]}/api/v4"
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ── Internal request helper ──────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> requests.Response:
        """Make an authenticated Clio API call and log the request + response."""
        url = path if path.startswith("http") else f"{self.api_base}/{path}"
        log.debug("→ %s %s  params=%s  body=%s", method.upper(), url, params,
                  json.dumps(json_body, default=str) if json_body else None)

        resp = requests.request(
            method,
            url,
            headers=self.headers,
            params=params,
            json=json_body,
        )

        # Log response — truncate large bodies to keep logs readable
        try:
            body = resp.json()
            body_str = json.dumps(body, default=str)
        except Exception:
            body_str = resp.text
        truncated = body_str[:600] + "…" if len(body_str) > 600 else body_str
        log.debug("← %s %s  body=%s", resp.status_code, url, truncated)

        return resp

    # ── Contacts ────────────────────────────────────────────────────────

    def get_contact_by_email(self, email: str) -> dict | None:
        """
        Search Clio contacts by email.
        Clio stores the email in `primary_email_address` (a string field),
        not inside email_addresses[].address.
        Strategy 1: query search then match on primary_email_address
        Strategy 2: page through all contacts and match client-side
        """
        fields = "id,name,first_name,last_name,email_addresses,primary_email_address"
        email_lower = email.strip().lower()

        def _matches(contact: dict) -> bool:
            # Check primary_email_address (top-level string)
            primary = (contact.get("primary_email_address") or "").lower()
            if primary == email_lower:
                return True
            # Fallback: check email_addresses array (address key)
            for addr in contact.get("email_addresses") or []:
                if isinstance(addr, dict):
                    if (addr.get("address") or "").lower() == email_lower:
                        return True
            return False

        # Strategy 1 — full-text query search
        resp = self._request("get", "contacts.json", params={"query": email, "fields": fields})
        resp.raise_for_status()
        for contact in resp.json().get("data", []):
            if _matches(contact):
                return contact

        # Strategy 2 — search by local part (before @)
        local_part = email.split("@")[0]
        resp2 = self._request("get", "contacts.json", params={"query": local_part, "fields": fields})
        resp2.raise_for_status()
        for contact in resp2.json().get("data", []):
            if _matches(contact):
                return contact

        # Strategy 3 — page through all contacts and match client-side
        page = 1
        while True:
            resp3 = self._request("get", "contacts.json",
                                  params={"fields": fields, "limit": 200, "page": page})
            resp3.raise_for_status()
            data = resp3.json()
            contacts = data.get("data", [])
            if not contacts:
                break
            for contact in contacts:
                if _matches(contact):
                    return contact
            meta = data.get("meta", {})
            if page >= 3 or not meta.get("next"):
                break
            page += 1

        return None

    def debug_list_contacts(self, limit: int = 10) -> list:
        """Return first N contacts with raw email_addresses — for debugging only."""
        resp = self._request("get", "contacts.json",
                             params={"fields": "id,name,email_addresses,primary_email_address", "limit": limit})
        resp.raise_for_status()
        return resp.json().get("data", [])

    # ── Matters ─────────────────────────────────────────────────────────

    def get_matters_for_contact(self, contact_id: int) -> list:
        """Return all matters linked to a contact."""
        resp = self._request("get", "matters.json", params={
            "client_id": contact_id,
            "fields": "id,display_number,description,status,client,responsible_attorney,custom_field_values",
        })
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_matter_by_id(self, matter_id: int) -> dict:
        """Fetch a single matter with its custom_field_values (including IDs)."""
        resp = self._request("get", f"matters/{matter_id}.json",
                             params={"fields": "id,display_number,custom_field_values"})
        resp.raise_for_status()
        return resp.json().get("data", {})

    def get_custom_field_value(self, cfv_id) -> dict:
        """Fetch a single custom_field_value by its ID (numeric suffix only)."""
        raw = str(cfv_id)
        numeric_id = raw.rsplit("-", 1)[-1] if "-" in raw else raw
        resp = self._request("get", f"custom_field_values/{numeric_id}.json",
                             params={"fields": "id,value,custom_field"})
        resp.raise_for_status()
        return resp.json().get("data", {})

    def update_matter(self, matter_id: int, custom_field_values: list) -> dict:
        """Patch a matter's custom field values."""
        resp = self._request("patch", f"matters/{matter_id}.json",
                             json_body={"data": {"custom_field_values": custom_field_values}})
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Matter update failed {resp.status_code}: {detail}")
        return resp.json()

    # ── Custom Fields ────────────────────────────────────────────────────

    def get_custom_fields(self, parent_type: str = "Matter") -> list:
        """List all custom fields for a given parent type (Matter or Contact)."""
        resp = self._request("get", "custom_fields.json",
                             params={"parent_type": parent_type, "fields": "id,name,field_type"})
        resp.raise_for_status()
        return resp.json().get("data", [])

    def create_custom_field(self, name: str, parent_type: str = "Matter", field_type: str = "TextLine") -> dict:
        """Create a new custom field in Clio."""
        resp = self._request("post", "custom_fields.json",
                             json_body={"data": {"name": name, "parent_type": parent_type, "field_type": field_type}})
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Custom field creation failed {resp.status_code}: {detail}")
        return resp.json().get("data", {})

    # ── Calendar ─────────────────────────────────────────────────────────

    def get_primary_calendar_id(self) -> int:
        """Return the ID of the current user's primary calendar."""
        resp = self._request("get", "calendars.json", params={"fields": "id,name,color"})
        resp.raise_for_status()
        calendars = resp.json().get("data", [])
        if not calendars:
            raise RuntimeError("No calendars found for this Clio account.")
        return calendars[0]["id"]

    def create_calendar_event(
        self,
        matter_id: int,
        summary: str,
        description: str,
        event_date_iso: str,
        calendar_id: int | None = None,
    ) -> dict:
        """Create an all-day calendar event linked to a matter."""
        if calendar_id is None:
            calendar_id = self.get_primary_calendar_id()
        if len(event_date_iso) == 10:
            event_date_iso = event_date_iso + "T00:00:00Z"
        resp = self._request("post", "calendar_entries.json", json_body={"data": {
            "summary": summary,
            "description": description,
            "start_at": event_date_iso,
            "end_at": event_date_iso,
            "all_day": True,
            "matter": {"id": matter_id},
            "calendar_owner": {"id": calendar_id},
        }})
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Calendar event failed {resp.status_code}: {detail}")
        return resp.json()

    # ── Document Templates ───────────────────────────────────────────────

    def get_document_templates(self) -> list:
        """List all document automation templates."""
        resp = self._request("get", "document_templates.json", params={"fields": "id,name"})
        resp.raise_for_status()
        return resp.json().get("data", [])

    # ── Document Automation ──────────────────────────────────────────────

    def generate_document(self, template_id: int, matter_id: int, filename: str = "Retainer Agreement") -> dict:
        """Trigger Clio document automation for a given template and matter."""
        resp = self._request("post", "document_automations.json", json_body={"data": {
            "document_template": {"id": template_id},
            "matter": {"id": matter_id},
            "filename": filename,
            "formats": ["pdf"],
        }})
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Document generation failed {resp.status_code}: {detail}")
        return resp.json()

    # ── Documents ────────────────────────────────────────────────────────

    def get_documents_for_matter(self, matter_id: int) -> list:
        """List all documents attached to a matter."""
        resp = self._request("get", "documents.json", params={
            "matter_id": matter_id,
            "fields": "id,name,created_at,latest_document_version",
            "order": "created_at(desc)",
        })
        resp.raise_for_status()
        return resp.json().get("data", [])

    def download_document(self, document_id: int) -> bytes:
        """Download a document by ID and return raw bytes."""
        resp = self._request("get", f"documents/{document_id}/download")
        resp.raise_for_status()
        return resp.content

    # ── Current User ─────────────────────────────────────────────────────

    def get_current_user(self) -> dict:
        """Return the authenticated user's profile."""
        resp = self._request("get", "users/who_am_i.json", params={"fields": "id,name,email"})
        resp.raise_for_status()
        return resp.json().get("data", {})
