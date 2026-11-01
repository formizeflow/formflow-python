import json
import unittest

from formflow import Client, FormFlowError, construct_event, sign_payload, verify_signature


def scripted(responses, calls):
    it = iter(responses)

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, dict(headers), body))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        status, payload, hdrs = r
        return status, hdrs, json.dumps(payload).encode() if payload is not None else b""

    return transport


class ClientTests(unittest.TestCase):
    def client(self, responses, calls, **kw):
        return Client("ff_test", base_url="https://x.test", transport=scripted(responses, calls), sleep=lambda s: calls.append(("sleep", s)), **kw)

    def test_auth_version_and_envelope(self):
        calls = []
        c = self.client([(200, {"data": {"id": "frm_1"}}, {})], calls)
        self.assertEqual(c.forms.get("frm_1"), {"id": "frm_1"})
        _, url, headers, _ = calls[0]
        self.assertEqual(url, "https://x.test/api/v1/forms/frm_1")
        self.assertEqual(headers["Authorization"], "Bearer ff_test")
        self.assertEqual(headers["FormFlow-Version"], "2026-10-01")
        self.assertNotIn("Idempotency-Key", headers)

    def test_retries_429_with_retry_after_and_reuses_idempotency_key(self):
        calls = []
        c = self.client([(429, {"error": {"code": "rate_limited", "message": "slow"}}, {"retry-after": "2"}), (200, {"data": {"id": "frm_1"}}, {})], calls)
        c.forms.publish("frm_1")
        posts = [x for x in calls if x[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2]["Idempotency-Key"], posts[1][2]["Idempotency-Key"])
        self.assertIn(("sleep", 2.0), calls)

    def test_gives_up_with_typed_error(self):
        calls = []
        c = self.client([(422, {"error": {"code": "validation_failed", "message": "bad", "details": {"issues": []}}}, {"x-request-id": "req_1"})], calls)
        with self.assertRaises(FormFlowError) as cm:
            c.forms.publish("frm_1")
        self.assertEqual((cm.exception.status, cm.exception.code, cm.exception.request_id), (422, "validation_failed", "req_1"))

    def test_network_errors_retry_then_raise(self):
        calls = []
        c = self.client([OSError("down")] * 2, calls, max_retries=1)
        with self.assertRaises(FormFlowError) as cm:
            c.forms.get("frm_1")
        self.assertEqual(cm.exception.code, "network_error")

    def test_paginates(self):
        calls = []
        c = self.client([(200, {"data": [{"id": 1}], "nextCursor": "c2"}, {}), (200, {"data": [{"id": 2}], "nextCursor": None}, {})], calls)
        self.assertEqual([f["id"] for f in c.forms.list(status="draft")], [1, 2])
        self.assertIn("cursor=c2", calls[1][1])


class WebhookTests(unittest.TestCase):
    body = '{"id":"evt_1","type":"form.published","created":1,"workspace_id":"ws","data":{}}'
    # computed independently with node:crypto using the server's scheme: HMAC-SHA256("whsec_test", "1700000000." + body)
    vector = "t=1700000000,v1=1797019d2af87623b436a19f93ebbf5be4717500a701adf21375c87b8335d38b"

    def test_known_vector_matches_server_scheme(self):
        self.assertEqual(sign_payload("whsec_test", self.body, 1700000000), self.vector)
        self.assertTrue(verify_signature("whsec_test", self.body, self.vector, now=1700000100))

    def test_rejects_tamper_wrong_secret_stale_and_junk(self):
        header = sign_payload("whsec_test", self.body, 1700000000)
        self.assertFalse(verify_signature("whsec_test", self.body + " ", header, now=1700000100))
        self.assertFalse(verify_signature("other", self.body, header, now=1700000100))
        self.assertFalse(verify_signature("whsec_test", self.body, header, now=1700001000))
        self.assertFalse(verify_signature("whsec_test", self.body, "garbage", now=1700000100))
        self.assertFalse(verify_signature("whsec_test", self.body, None))

    def test_accepts_rotated_secret_header_and_bytes(self):
        good = sign_payload("new", self.body, 1700000000)
        header = "t=1700000000,v1=" + "0" * 64 + "," + good.split(",", 1)[1]
        self.assertTrue(verify_signature("new", self.body.encode(), header, now=1700000000))

    def test_construct_event(self):
        header = sign_payload("s", self.body)
        self.assertEqual(construct_event("s", self.body, header)["type"], "form.published")
        with self.assertRaises(FormFlowError):
            construct_event("s", self.body, "t=1,v1=00")


if __name__ == "__main__":
    unittest.main()
