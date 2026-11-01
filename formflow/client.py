from __future__ import annotations

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
