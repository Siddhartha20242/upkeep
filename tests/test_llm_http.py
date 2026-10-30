"""The model client against a real local HTTP server (so the urllib code path itself is exercised)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from upkeep.llm import Client, LLMError, Reply, client_for


class Handler(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list = []

    def log_message(self, *a):  # keep test output quiet
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"data": [{"id": "m1"}, {"id": "m2"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        Handler.seen.append({"auth": self.headers.get("Authorization"), "payload": payload})
        if Handler.mode == "echo-key-401":   # a server that reflects the Authorization header into its error body
            self._send(401, {"error": f"bad credentials {self.headers.get('Authorization')}"})
        elif Handler.mode == "garbage":
            self._send(200, b"not json at all")
        elif Handler.mode == "truncated":
            self._send(200, {"choices": [{"message": {"content": "cut", "reasoning_content": "thinking"}, "finish_reason": "length"}],
                             "usage": {"total_tokens": 9}})
        else:
            self._send(200, {"choices": [{"message": {"content": "hi", "tool_calls": [{"id": "1", "type": "function",
                             "function": {"name": "finish", "arguments": "{}"}}]}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}})


@pytest.fixture
def server():
    Handler.mode, Handler.seen = "ok", []
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def test_chat_round_trip_sends_bearer_key_and_parses_tool_calls_and_usage(server):
    c = Client(api_key="secret-key-123", model="m", base_url=server)
    r = c.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "finish"}}], temperature=0.3)
    assert r.text == "hi" and r.tool_calls[0]["function"]["name"] == "finish" and r.usage["total_tokens"] == 5
    assert not r.truncated and r.latency_s >= 0
    sent = Handler.seen[0]
    assert sent["auth"] == "Bearer secret-key-123" and sent["payload"]["model"] == "m" and sent["payload"]["temperature"] == 0.3


def test_list_models(server):
    assert Client(api_key="k", model="m", base_url=server).list_models() == ["m1", "m2"]


def test_error_body_that_reflects_the_key_is_redacted(server):
    Handler.mode = "echo-key-401"
    with pytest.raises(LLMError) as e:
        Client(api_key="secret-key-123", model="m", base_url=server).chat([{"role": "user", "content": "x"}])
    assert "401" in str(e.value) and "secret-key-123" not in str(e.value) and "***" in str(e.value)


def test_truncated_reply_and_reasoning_are_exposed(server):
    Handler.mode = "truncated"
    r = Client(api_key="k", model="m", base_url=server).chat([{"role": "user", "content": "x"}])
    assert r.truncated and r.reasoning == "thinking" and r.text == "cut"


def test_malformed_server_response_is_an_llm_error_not_a_crash(server):
    Handler.mode = "garbage"
    with pytest.raises((LLMError, ValueError)):
        Client(api_key="k", model="m", base_url=server).chat([{"role": "user", "content": "x"}])


def test_connection_refused_is_reported_as_a_network_error():
    with pytest.raises(LLMError, match="network error"):
        Client(api_key="k", model="m", base_url="http://127.0.0.1:9/v1", timeout=2).chat([{"role": "user", "content": "x"}])


def test_reply_properties_on_an_empty_message():
    r = Reply(message={})
    assert r.text == "" and r.reasoning == "" and r.tool_calls == [] and not r.truncated


@pytest.mark.parametrize("provider,env", [("baseten", "BASETEN_API_KEY"), ("meta", "META_API_KEY")])
def test_client_for_each_provider_needs_its_own_key(monkeypatch, provider, env):
    monkeypatch.delenv(env, raising=False)
    with pytest.raises(LLMError, match=env):
        client_for(provider)
    monkeypatch.setenv(env, "k")
    assert client_for(provider, "custom-model").model == "custom-model"
