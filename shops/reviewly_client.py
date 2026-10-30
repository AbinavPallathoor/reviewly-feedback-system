"""Minimal Reviewly API client (standard library only) used by the example shops."""
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request


class ReviewlyError(Exception):
    def __init__(self, status, reason, data=None):
        super().__init__(f"{status}: {reason}")
        self.status, self.reason, self.data = status, reason, data or {}


class ReviewlyClient:
    def __init__(self, base_url, public_key, secret, on_call=None):
        self.base, self.key, self.secret = base_url.rstrip("/"), public_key, secret
        self.on_call = on_call      # callback(method, path, status, summary) for the shop's own log
        self.last_request = None    # kept so the demo panel can replay it

    def sign(self, method, path, body, ts, nonce, secret=None):
        msg = f"{ts}.{nonce}.{method}.{path}.{body}"
        return hmac.new((secret or self.secret).encode(), msg.encode(), hashlib.sha256).hexdigest()

    def build(self, method, path, payload=None, secret=None):
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        headers = {"Content-Type": "application/json", "X-Reviewly-Key": self.key,
                   "X-Reviewly-Timestamp": ts, "X-Reviewly-Nonce": nonce,
                   "X-Reviewly-Signature": self.sign(method, path, body, ts, nonce, secret)}
        return {"method": method, "path": path, "body": body, "headers": headers}

    def send(self, req, remember=True):
        if remember:
            self.last_request = req
        http = urllib.request.Request(self.base + req["path"], data=req["body"].encode() if req["body"] else None,
                                      headers=req["headers"], method=req["method"])
        try:
            with urllib.request.urlopen(http, timeout=8) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            self._log(req, 0, f"Reviewly unreachable ({getattr(e, 'reason', e)})")
            raise ReviewlyError(0, "Reviewly is unreachable right now.")
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {"error": f"Non-JSON response (HTTP {status})"}
        if status >= 400:
            reason = data.get("error") or f"HTTP {status}"
            self._log(req, status, reason)
            raise ReviewlyError(status, reason, data)
        self._log(req, status, "ok")
        return data

    def call(self, method, path, payload=None):
        return self.send(self.build(method, path, payload))

    def _log(self, req, status, summary):
        if self.on_call:
            self.on_call(req["method"], req["path"], status, summary, req["body"])
