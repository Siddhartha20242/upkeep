import io
import json
import urllib.error

import pytest

from upkeep import llm
from upkeep.llm import Client, LLMError, load_env

SECRET = "sk-test-SECRET-123"


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)
    with pytest.raises(LLMError):
        Client()


def test_load_env_does_not_override_and_ignores_comments(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# comment\nBASETEN_API_KEY=from-file\nOTHER='quoted value'\n\nbad line\n")
    monkeypatch.setenv("BASETEN_API_KEY", "from-shell")
    monkeypatch.delenv("OTHER", raising=False)
    load_env(f)
    assert llm.os.environ["BASETEN_API_KEY"] == "from-shell"
    assert llm.os.environ["OTHER"] == "quoted value"
    monkeypatch.delenv("OTHER", raising=False)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_chat_parses_reply_and_sends_expected_request(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return FakeResp(json.dumps({
            "model": "m",
            "choices": [{"message": {"role": "assistant", "content": "pong"}}],
            "usage": {"total_tokens": 7},
        }).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    r = Client(api_key=SECRET).chat([{"role": "user", "content": "hi"}])
    assert r.text == "pong" and r.usage == {"total_tokens": 7}
    assert seen["url"] == "https://inference.baseten.co/v1/chat/completions"
    assert seen["body"]["model"] == "meta-models/muse-spark-open-1026"
    assert seen["auth"] == f"Bearer {SECRET}"


def test_http_error_never_leaks_the_key(monkeypatch):
    def boom(req, timeout):
        raise urllib.error.HTTPError(
            req.full_url, 401, "unauthorized", {}, io.BytesIO(f"bad key {SECRET}".encode())
        )

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    with pytest.raises(LLMError) as e:
        Client(api_key=SECRET).chat([{"role": "user", "content": "hi"}])
    assert SECRET not in str(e.value)
    assert "401" in str(e.value)


def test_unexpected_shape_is_an_error(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout: FakeResp(b'{"nope": 1}'))
    with pytest.raises(LLMError):
        Client(api_key=SECRET).chat([{"role": "user", "content": "hi"}])
