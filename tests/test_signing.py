import json
import subprocess
import sys
import time

import pytest

from shopee_affiliate import signing

URL = "https://affiliate.shopee.vn/api/v3/offer/product?item_id=26609048170"
HEADERS = {"x-sap-ri": "r" * 52, "x-sap-sec": "s" * 712}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    path = tmp_path / "harness.cjs"
    path.write_text("// fixture")
    monkeypatch.setattr(signing.shutil, "which", lambda name: "/usr/bin/node")
    return path


def test_signer_protocol(harness, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps(HEADERS)

    monkeypatch.setattr(signing, "_run_signer", run)
    assert signing.sign_headers(harness, URL) == HEADERS
    args, kwargs = calls[0]
    assert args == ["/usr/bin/node", "--max-old-space-size=192", str(harness.resolve()), "--headers"]
    assert json.loads(kwargs["input"]) == {"url": URL, "body": None}
    assert kwargs["timeout"] == 15


@pytest.mark.parametrize("url", ["http://affiliate.shopee.vn/api/v3/offer/product?item_id=1",
    URL + "&other=1", URL + "#fragment", "https://evil.test/api/v3/offer/product?item_id=1",
    "https://affiliate.shopee.vn/api/v3/offer/product?item_id=01", None])
def test_invalid_endpoint(harness, url):
    with pytest.raises(signing.SigningError):
        signing.sign_headers(harness, url)


@pytest.mark.parametrize("output", ["secret", "[]", "{}", '{"x-sap-ri":"a"}',
    json.dumps({**HEADERS, "Cookie": "secret"}), json.dumps({**HEADERS, "x-sap-sec": "a\r\nb"}),
    json.dumps({**HEADERS, "x-sap-sec": 123}), json.dumps({**HEADERS, "x-sap-sec": "a" * 28}),
    json.dumps({**HEADERS, "x-sap-sec": "a" * 16385}), " " * 32769,
    '{"x-sap-ri":"a","x-sap-ri":"b","x-sap-sec":"c"}'])
def test_invalid_output(harness, monkeypatch, output):
    monkeypatch.setattr(signing, "_run_signer", lambda *args, **kwargs: output)
    with pytest.raises(signing.SigningError) as exc:
        signing.sign_headers(harness, URL)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("secret", 15), OSError("secret")])
def test_execution_failure(harness, monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(signing, "_run_signer", run)
    with pytest.raises(signing.SigningError) as exc:
        signing.sign_headers(harness, URL)
    assert "secret" not in str(exc.value)


def test_nonzero_exit():
    with pytest.raises(signing.SigningError, match="failed"):
        signing._run_signer([sys.executable, "-c", "raise SystemExit(1)"], input="{}", timeout=1)



def test_missing_node(harness, monkeypatch):
    monkeypatch.setattr(signing.shutil, "which", lambda name: None)
    with pytest.raises(signing.SigningError, match="Node"):
        signing.sign_headers(harness, URL)


def test_missing_harness(tmp_path):
    with pytest.raises(signing.SigningError, match="file"):
        signing.sign_headers(tmp_path / "absent.cjs", URL)


def test_streaming_stderr_discard_and_environment(monkeypatch):
    monkeypatch.setenv("SHOPEE_COOKIES", "must-not-inherit")
    script = "import os,sys; assert 'SHOPEE_COOKIES' not in os.environ; sys.stderr.write('x'*1000000); print(sys.stdin.read())"
    assert signing._run_signer([sys.executable, "-c", script], input="{}", timeout=2).strip() == "{}"


@pytest.mark.parametrize("script,timeout,match", [
    ("import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(10)", 2, "size"),
    ("import time; time.sleep(10)", 0.1, "timed out"),
    ("import sys,time; sys.stdout.close(); time.sleep(10)", 0.1, "timed out"),
])
def test_streaming_limit_and_cleanup(monkeypatch, script, timeout, match):
    children = []
    popen = subprocess.Popen

    def tracked(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(signing.subprocess, "Popen", tracked)
    started = time.monotonic()
    with pytest.raises(signing.SigningError, match=match):
        signing._run_signer([sys.executable, "-c", script], input="{}", timeout=timeout)
    assert time.monotonic() - started < 3
    assert all(child.poll() is not None for child in children)
    assert all(child.stdout.closed and child.stdin.closed for child in children)
