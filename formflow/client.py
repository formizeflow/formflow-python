from __future__ import annotations

import base64
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from .errors import FormFlowError

DEFAULT_VERSION = "2026-10-01"
JSON = Dict[str, Any]
# (method, url, headers, body, timeout) -> (status, response headers with lower-case names, body bytes)
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], Tuple[int, Mapping[str, str], bytes]]


def _urllib_transport(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


class Client:
    """One instance per API key. Retries 429, 5xx and network errors with exponential backoff and jitter
    (Retry-After wins when present). Every POST carries an Idempotency-Key that is reused across its retries."""

    def __init__(self, api_key: str, base_url: str = "https://app.formflow.dev", version: str = DEFAULT_VERSION,
                 max_retries: int = 3, timeout: float = 30.0, workspace_id: Optional[str] = None,
                 transport: Optional[Transport] = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key, self.base_url, self.version = api_key, base_url.rstrip("/"), version
        self.max_retries, self.timeout, self.workspace_id = max_retries, timeout, workspace_id
        self._transport, self._sleep = transport or _urllib_transport, sleep
        self.forms = Forms(self)
        self.submissions = Submissions(self)
        self.results = Results(self)
        self.payments = Payments(self)
        self.webhooks = Webhooks(self)
        self.members = Members(self)
        self.api_keys = ApiKeys(self)
        self.audit_events = AuditEvents(self)
        self.tables = Tables(self)
        self.workflows = Workflows(self)
        self.themes = Themes(self)
        self.ai = Ai(self)
        self.exports = Exports(self)
        self.files = Files(self)

    def request(self, method: str, path: str, query: Optional[Mapping[str, Any]] = None, body: Any = None,
                idempotency_key: Optional[str] = None) -> Any:
        """Returns the parsed JSON body (the envelope), or None for 204."""
        url = f"{self.base_url}/api/v1{path}"
        q = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else v)
             for k, v in (query or {}).items() if v is not None}
        if q:
            url += "?" + urllib.parse.urlencode(q)
        headers = {"Authorization": f"Bearer {self.api_key}", "FormFlow-Version": self.version, "Accept": "application/json"}
        if self.workspace_id:
            headers["FormFlow-Workspace"] = self.workspace_id
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method == "POST":
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        for attempt in range(self.max_retries + 1):
            try:
                status, rh, raw = self._transport(method, url, headers, data, self.timeout)
            except OSError as e:
                if attempt >= self.max_retries:
                    raise FormFlowError(0, "network_error", str(e)) from e
                self._sleep(_backoff(attempt))
                continue
            if 200 <= status < 300:
                return json.loads(raw) if status != 204 and raw else None
            retry_after = _float(rh.get("retry-after"))
            if (status == 429 or status >= 500) and attempt < self.max_retries:
                self._sleep(retry_after if retry_after is not None else _backoff(attempt))
                continue
            try:
                err = json.loads(raw).get("error", {})
            except (ValueError, AttributeError):
                err = {}
            raise FormFlowError(status, err.get("code", "http_error"), err.get("message", f"HTTP {status}"),
                                err.get("details"), rh.get("x-request-id"), retry_after)
        raise AssertionError("unreachable")

    def data(self, method: str, path: str, **kw: Any) -> Any:
        r = self.request(method, path, **kw)
        return None if r is None else r.get("data")

    def paginate(self, path: str, **query: Any) -> Iterator[Any]:
        """Iterate every item of a cursor-paginated list endpoint."""
        cursor = query.pop("cursor", None)
        while True:
            page = self.request("GET", path, query={**query, "cursor": cursor})
            yield from page["data"]
            cursor = page.get("nextCursor")
            if not cursor:
                return


def _backoff(attempt: int) -> float:
    return min(30.0, 0.5 * 2 ** attempt) * (0.75 + random.random() * 0.5)


def _float(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


class _Resource:
    def __init__(self, c: Client) -> None:
        self._c = c


class Forms(_Resource):
    def list(self, **query: Any) -> Iterator[JSON]:
        """status, folderId, q. Auto-paginates."""
        return self._c.paginate("/forms", **query)

    def get(self, form_id: str) -> JSON:
        return self._c.data("GET", f"/forms/{form_id}")

    def create(self, name: str, definition: Optional[JSON] = None, template_id: Optional[str] = None, **extra: Any) -> JSON:
        return self._c.data("POST", "/forms", body={"name": name, "definition": definition, "templateId": template_id, **extra})

    def update(self, form_id: str, **patch: Any) -> JSON:
        """Replacing `definition` needs expectedRevision (409 conflict if the draft moved on)."""
        return self._c.data("PATCH", f"/forms/{form_id}", body=patch)

    def operations(self, form_id: str, operations: List[JSON], expected_revision: Optional[int] = None) -> JSON:
        return self._c.data("POST", f"/forms/{form_id}/operations", body={"operations": operations, "expectedRevision": expected_revision})

    def publish(self, form_id: str) -> JSON:
        return self._c.data("POST", f"/forms/{form_id}/publish", body={})

    def delete(self, form_id: str) -> None:
        self._c.data("DELETE", f"/forms/{form_id}")


class Submissions(_Resource):
    def list(self, form_id: str, **query: Any) -> Iterator[JSON]:
        """status, from, to, q, sort. Auto-paginates."""
        return self._c.paginate(f"/forms/{form_id}/submissions", **query)

    def get(self, submission_id: str) -> JSON:
        return self._c.data("GET", f"/submissions/{submission_id}")

    def export(self, form_id: str, format: str = "csv", **filter: Any) -> JSON:
        return self._c.data("POST", f"/forms/{form_id}/exports", body={"format": format, "filter": filter or None})


class Results(_Resource):
    def triage(self, form_id: str) -> JSON:
        return self._c.data("GET", f"/forms/{form_id}/triage")

    def bulk_triage(self, ids: List[str], **patch: Any) -> JSON:
        """status, assigneeId, addTags, removeTags."""
        return self._c.data("POST", "/submissions/bulk-triage", body={"ids": ids, **patch})

    def restore_version(self, form_id: str, version: int) -> JSON:
        """Replaces the draft; the live form is unchanged until publish."""
        return self._c.data("POST", f"/forms/{form_id}/versions/{version}/restore", body={})

    def create_share_link(self, form_id: str, name: str, **opts: Any) -> JSON:
        """The returned `url` is a bearer capability: treat it like a credential."""
        return self._c.data("POST", f"/forms/{form_id}/share-links", body={"name": name, **opts})


class Payments(_Resource):
    def list(self, **query: Any) -> JSON:
        return self._c.data("GET", "/payments", query=query)

    def refund(self, payment_id: str, confirm_amount_minor: int, amount_minor: Optional[int] = None, reason: Optional[str] = None) -> JSON:
        """confirm_amount_minor must equal the amount being refunded (the remaining balance when amount_minor is omitted)."""
        return self._c.data("POST", f"/payments/{payment_id}/refund",
                            body={"confirmAmountMinor": confirm_amount_minor, "amountMinor": amount_minor, "reason": reason})


class Webhooks(_Resource):
    def list(self) -> List[JSON]:
        return self._c.data("GET", "/webhooks")

    def create(self, url: str, events: List[str], form_id: Optional[str] = None) -> JSON:
        """The signing `secret` is returned once."""
        return self._c.data("POST", "/webhooks", body={"url": url, "events": events, "formId": form_id})


class Members(_Resource):
    def list(self) -> List[JSON]:
        return self._c.data("GET", "/members")

    def invite(self, email: str, role: str) -> JSON:
        return self._c.data("POST", "/members", body={"email": email, "role": role})

    def set_role(self, membership_id: str, role: str) -> JSON:
        return self._c.data("PATCH", f"/members/{membership_id}", body={"role": role})

    def remove(self, membership_id: str) -> None:
        self._c.data("DELETE", f"/members/{membership_id}")


class ApiKeys(_Resource):
    def list(self) -> List[JSON]:
        return self._c.data("GET", "/api-keys")

    def create(self, name: str, scopes: List[str], expires_at: Optional[int] = None) -> JSON:
        """The full `key` is returned once."""
        return self._c.data("POST", "/api-keys", body={"name": name, "scopes": scopes, "expiresAt": expires_at})

    def revoke(self, key_id: str) -> None:
        self._c.data("DELETE", f"/api-keys/{key_id}")


class AuditEvents(_Resource):
    def list(self, **query: Any) -> Iterator[JSON]:
        """action, resourceType, from, to (epoch ms or ISO 8601). Auto-paginates; each event has actorName."""
        return self._c.paginate("/audit-events", **query)

    def list_org(self, **query: Any) -> Iterator[JSON]:
        """Every workspace of the organization. Org owners and admins only."""
        return self._c.paginate("/org/audit-events", **query)

    def export(self, format: str = "csv", scope: str = "workspace", **filter: Any) -> JSON:
        """Starts a CSV or JSON export; poll it with exports.get or exports.wait."""
        return self._c.data("POST", "/audit-events/export", body={"format": format, "scope": scope, **filter})


class Tables(_Resource):
    def list(self) -> List[JSON]:
        return self._c.data("GET", "/tables")

    def get(self, table_id: str) -> JSON:
        return self._c.data("GET", f"/tables/{table_id}")

    def create(self, name: str, columns: List[JSON]) -> JSON:
        return self._c.data("POST", "/tables", body={"name": name, "columns": columns})

    def update(self, table_id: str, **patch: Any) -> JSON:
        return self._c.data("PATCH", f"/tables/{table_id}", body=patch)

    def delete(self, table_id: str) -> None:
        self._c.data("DELETE", f"/tables/{table_id}")

    def rows(self, table_id: str, **query: Any) -> Iterator[JSON]:
        return self._c.paginate(f"/tables/{table_id}/rows", **query)

    def add_rows(self, table_id: str, rows: Any) -> List[JSON]:
        """One row (a dict) or a list of rows."""
        return self._c.data("POST", f"/tables/{table_id}/rows", body=rows)

    def update_row(self, table_id: str, row_id: str, cells: JSON) -> JSON:
        return self._c.data("PATCH", f"/tables/{table_id}/rows/{row_id}", body=cells)

    def delete_row(self, table_id: str, row_id: str) -> None:
        self._c.data("DELETE", f"/tables/{table_id}/rows/{row_id}")


class Workflows(_Resource):
    def list(self, form_id: Optional[str] = None) -> List[JSON]:
        return self._c.data("GET", "/workflows", query={"formId": form_id})

    def get(self, workflow_id: str) -> JSON:
        return self._c.data("GET", f"/workflows/{workflow_id}")

    def create(self, **body: Any) -> JSON:
        return self._c.data("POST", "/workflows", body=body)

    def update(self, workflow_id: str, **patch: Any) -> JSON:
        return self._c.data("PATCH", f"/workflows/{workflow_id}", body=patch)

    def delete(self, workflow_id: str) -> None:
        self._c.data("DELETE", f"/workflows/{workflow_id}")

    def executions(self, workflow_id: str) -> List[JSON]:
        return self._c.data("GET", f"/workflows/{workflow_id}/executions")


class Themes(_Resource):
    def list(self) -> List[JSON]:
        return self._c.data("GET", "/themes")

    def presets(self) -> List[JSON]:
        return self._c.data("GET", "/themes/presets")

    def get(self, theme_id: str) -> JSON:
        return self._c.data("GET", f"/themes/{theme_id}")

    def create(self, name: str, definition: JSON) -> JSON:
        return self._c.data("POST", "/themes", body={"name": name, "definition": definition})

    def update(self, theme_id: str, **patch: Any) -> JSON:
        return self._c.data("PATCH", f"/themes/{theme_id}", body=patch)

    def delete(self, theme_id: str) -> None:
        self._c.data("DELETE", f"/themes/{theme_id}")


class Ai(_Resource):
    def generate_form(self, prompt: str, **opts: Any) -> JSON:
        """Prompt to a validated form definition. opts: presentation, audience, create={"name": ..., "folderId": ...}. Uses AI credits."""
        return self._c.data("POST", "/ai/generate-form", body={"prompt": prompt, **opts})


class Exports(_Resource):
    def get(self, export_id: str) -> JSON:
        return self._c.data("GET", f"/exports/{export_id}")

    def wait(self, export_id: str, interval: float = 1.5, timeout: float = 120.0) -> JSON:
        """Polls until the export is done or failed; `url` is a short-lived download link when done."""
        waited = 0.0
        while True:
            job = self.get(export_id)
            if job["export"]["status"] in ("done", "failed"):
                return job
            if waited >= timeout:
                raise TimeoutError(f"export {export_id} did not finish in {timeout}s")
            self._c._sleep(interval)
            waited += interval


class Files(_Resource):
    """The workspace media library (images, video, fonts)."""

    def list(self, **query: Any) -> JSON:
        """search, kind (image|video|font), cursor. Returns {"data": [...], "nextCursor": ...}."""
        return self._c.request("GET", "/media", query=query)

    def upload(self, filename: str, content: Optional[bytes] = None, from_url: Optional[str] = None, **meta: Any) -> JSON:
        """Upload bytes (about 1.3 MB; larger files use multipart POST /api/media) or have the server fetch a public from_url."""
        if (content is None) == (from_url is None):
            raise ValueError("pass exactly one of content and from_url")
        body: JSON = {"filename": filename, **meta}
        if content is not None:
            body["contentBase64"] = base64.b64encode(content).decode("ascii")
        else:
            body["fromUrl"] = from_url
        return self._c.data("POST", "/media", body=body)
