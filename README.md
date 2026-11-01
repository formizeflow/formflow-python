# formizeflow (Python)

[![CI](https://github.com/formizeflow/formflow-python/actions/workflows/ci.yml/badge.svg)](https://github.com/formizeflow/formflow-python/actions/workflows/ci.yml)

Small typed client for the FormFlow REST API. Standard library only, Python 3.9+. Tested in CI on Python 3.9 to 3.13.

```sh
pip install formizeflow   # import name: formflow
```

```python
from formflow import Client, FormFlowError

ff = Client(api_key="ff_live_...", base_url="https://forms.example.com")
form = ff.forms.create("Feedback")
ff.forms.operations(form["id"], [{"op": "add_field", "field": {"id": "nps", "type": "nps", "label": "How likely are you to recommend us?"}}])
ff.forms.publish(form["id"])

for sub in ff.submissions.list(form["id"], status="completed"):   # pages automatically
    print(sub["id"], sub["answers"])
```

- **Auth**: `Authorization: Bearer <key>`; `workspace_id=` sets `FormFlow-Workspace` for user or OAuth credentials with several workspaces. Pin the API with `version=`.
- **Retries**: 429, 5xx and network errors, up to `max_retries` (default 3), exponential backoff with jitter; `Retry-After` is honored.
- **Idempotency**: every POST sends an `Idempotency-Key`. Pass your own as `idempotency_key=` to `Client.request` to make a retry across processes safe; the same key is reused across the automatic retries.
- **Errors**: `FormFlowError` with `status`, `code`, `message`, `details` (publish failures list `details["issues"]`), `request_id`, `retry_after`.
- **Anything not wrapped**: `ff.data("POST", "/approvals/apr_1/cancel", body={})` or `ff.request(...)`. The spec is `openapi.json`.

## Webhooks

```python
from formflow import construct_event, FormFlowError

def handle(raw_body: bytes, headers):          # raw body, not re-serialized JSON
    event = construct_event(SECRET, raw_body, headers.get("FormFlow-Signature"))
    if event["type"] == "submission.completed":
        ...
```

Scheme: `FormFlow-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "t.body")>`, 5 minute replay window, several `v1` values accepted during secret rotation.

## Tests

`python -m unittest discover -s tests` from this directory.
