"""Demo site function (api/index.py) and the upload path (pipeline/demo.py), offline.

- nothing but /login is reachable without the password; no password set means no entry;
- signed in: the app, persona data and workbooks are served; source and config are not;
- an upload returns the recap payload; a bad workbook returns its problems;
- the deploy folder carries only what the site runs.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline import demo
from pipeline.ingest import load_month
from pipeline.workbook import write_workbook
from pipeline.write import ModelReply
from tests.conftest import REPO
from tests.recap_fixtures import good_json

_real_run_upload = demo.run_upload


def _good_copy() -> str:
    return good_json()                      # a known-good recap for Riley (p09)


def _offline(data):
    return _real_run_upload(data, call=fake_call)


def fake_call(cfg, system, user):
    return ModelReply(_good_copy(), "end_turn", {"input_tokens": 1000, "output_tokens": 500, "model": cfg.model})


@pytest.fixture(scope="module")
def server():
    spec = importlib.util.spec_from_file_location("site_function", REPO / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def req(port, method, path, body=None, cookie=None, ctype=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {}
    if cookie:
        headers["Cookie"] = cookie
    if ctype:
        headers["Content-Type"] = ctype
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read()
    return r.status, dict(r.getheaders()), data


def login(port, password="team-pass"):
    status, headers, _ = req(port, "POST", "/login", f"password={password}", ctype="application/x-www-form-urlencoded")
    return status, headers.get("Location"), (headers.get("Set-Cookie") or "").split(";")[0]


def test_signed_out_reaches_only_the_login_page(server, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "team-pass")
    assert req(server, "GET", "/login")[0] == 200
    for path in ("/", "/gallery", "/downloads/recap_template.xlsx", "/emails/p01.html", "/config/model.yaml", "/api/index.py"):
        status, headers, _ = req(server, "GET", path)
        assert (status, headers.get("Location")) in ((303, "/login"), (401, None)), path
    assert req(server, "GET", "/data/personas.json")[0] == 401
    assert req(server, "POST", "/api/recap", b"x")[0] == 401


def test_wrong_or_unset_password_never_signs_in(server, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "team-pass")
    assert login(server, "nope")[:2] == (303, "/login?e=1")
    monkeypatch.setenv("SITE_PASSWORD", "")
    assert login(server, "")[:2] == (303, "/login?e=setup")
    assert req(server, "GET", "/", cookie="recap_demo=anything")[0] == 303


def test_signed_in_gets_the_site_but_not_the_source(server, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "team-pass")
    status, where, cookie = login(server)
    assert (status, where) == (303, "/")
    assert req(server, "GET", "/", cookie=cookie)[0] == 200
    personas = json.loads(req(server, "GET", "/data/personas.json", cookie=cookie)[2])
    assert [p["id"] for p in personas] == [f"p{i:02d}" for i in range(1, 11)]
    for p in personas:
        assert req(server, "GET", f"/downloads/{p['workbook']}", cookie=cookie)[0] == 200
        status, headers, body = req(server, "GET", f"/emails/{p['email']}", cookie=cookie)
        assert status == 200 and b"This isn't your account statement." in body and b"{{" not in body
    status, headers, body = req(server, "GET", "/gallery", cookie=cookie)
    assert status == 200 and b"Recap review gallery" in body
    for path in ("/config/model.yaml", "/pipeline/write.py", "/downloads/..%2Fconfig%2Fmodel.yaml", "/site/app.html",
                 "/emails/..%2Fgallery.html", "/emails/p99.html"):
        assert req(server, "GET", path, cookie=cookie)[0] == 404, path
    # a changed password ends old sessions
    monkeypatch.setenv("SITE_PASSWORD", "new-pass")
    assert req(server, "GET", "/", cookie=cookie)[0] == 303


def test_upload_returns_a_recap_or_the_workbook_problems(server, monkeypatch, fresh):
    monkeypatch.setenv("SITE_PASSWORD", "team-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr(demo, "run_upload", _offline)          # the real pipeline, with a fake Claude
    cookie = login(server)[2]
    xlsx = write_workbook(load_month(fresh / "raw" / "2026-10" / "p09"))
    status, _, body = req(server, "POST", "/api/recap", xlsx, cookie=cookie, ctype="application/octet-stream")
    out = json.loads(body)
    assert status == 200 and out["status"] == "passed" and out["first_name"] == "Riley"
    assert "down 4.1%" in out["recap_text"] and out["facts"] and out["assumptions"] == []
    status, _, body = req(server, "POST", "/api/recap", b"not a workbook", cookie=cookie, ctype="application/octet-stream")
    assert status == 400 and "not an Excel workbook" in json.loads(body)["problems"][0]


def test_no_api_key_is_a_clear_error(server, monkeypatch):
    monkeypatch.setenv("SITE_PASSWORD", "team-pass")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cookie = login(server)[2]
    status, _, body = req(server, "POST", "/api/recap", b"PK", cookie=cookie)
    assert status == 503 and "API key" in json.loads(body)["problems"][0]


def test_persona_data_matches_the_configured_model_and_prompt():
    from pipeline.write import ModelConfig
    cfg = ModelConfig.load()
    for p in json.loads((REPO / "site" / "data" / "personas.json").read_text(encoding="utf-8")):
        assert (p["model"], p["prompt_version"], p["status"]) == (cfg.model, cfg.prompt_version, "passed"), p["id"]
        assert (REPO / "site" / "downloads" / p["workbook"]).is_file()


def test_deploy_folder_has_only_what_the_site_runs(tmp_path):
    from scripts.build_site import assemble_deploy
    dest = tmp_path / "deploy"
    assemble_deploy(dest)
    assert not (dest / "data").exists() and not (dest / "mock").exists() and not (dest / "pyproject.toml").exists()
    reqs = (dest / "requirements.txt").read_text().lower()
    for heavy in ("polars", "numpy", "scipy"):
        assert heavy not in reqs
    cfg = json.loads((dest / "vercel.json").read_text())
    assert cfg["rewrites"] == [{"source": "/(.*)", "destination": "/api/index"}]
    assert cfg["outputDirectory"] == "public" and [p.name for p in (dest / "public").iterdir()] == ["robots.txt"]
    assert ".env*" in (dest / ".vercelignore").read_text().splitlines()
