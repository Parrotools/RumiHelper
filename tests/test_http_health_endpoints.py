import json
from types import SimpleNamespace

from rumi_helper import cli
from rumi_helper.services import runtime as runtime_module


class FakeHealth:
    def __init__(self):
        self.readiness_calls = 0
        self.health_calls = 0

    async def check_readiness(self):
        self.readiness_calls += 1
        return {
            "ok": True,
            "database": {"ok": True, "detail": "internal"},
            "llm": {"ok": True, "detail": "internal"},
        }

    async def check(self, *, include_diagnostics: bool):
        self.health_calls += 1
        assert include_diagnostics is False
        return {
            "ok": False,
            "github": {"ok": False, "detail": "internal"},
            "qq": {"ok": False, "detail": "internal"},
        }


def _request(authorization: str = ""):
    return SimpleNamespace(headers={"authorization": authorization})


def _payload(response):
    return json.loads(response.body)


async def test_readyz_rejects_unauthorized_request_before_probe(monkeypatch):
    health = FakeHealth()
    monkeypatch.setattr(cli, "_ONEBOT_ACCESS_TOKEN", "secret")
    monkeypatch.setattr(
        runtime_module,
        "get_runtime",
        lambda: SimpleNamespace(health=health),
    )

    response = await cli.readyz(_request())

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert _payload(response) == {"ok": False}
    assert health.readiness_calls == 0


async def test_readyz_authorized_response_is_minimal(monkeypatch):
    health = FakeHealth()
    monkeypatch.setattr(cli, "_ONEBOT_ACCESS_TOKEN", "secret")
    monkeypatch.setattr(
        runtime_module,
        "get_runtime",
        lambda: SimpleNamespace(health=health),
    )

    response = await cli.readyz(_request("Bearer secret"))

    assert response.status_code == 200
    assert _payload(response) == {"ok": True}
    assert health.readiness_calls == 1


async def test_healthz_authorized_failure_is_minimal(monkeypatch):
    health = FakeHealth()
    monkeypatch.setattr(cli, "_ONEBOT_ACCESS_TOKEN", "secret")
    monkeypatch.setattr(
        runtime_module,
        "get_runtime",
        lambda: SimpleNamespace(health=health),
    )

    response = await cli.healthz(_request("Bearer secret"))

    assert response.status_code == 503
    assert _payload(response) == {"ok": False}
    assert health.health_calls == 1


async def test_health_probes_remain_open_when_no_token_is_configured(monkeypatch):
    health = FakeHealth()
    monkeypatch.setattr(cli, "_ONEBOT_ACCESS_TOKEN", "")
    monkeypatch.setattr(
        runtime_module,
        "get_runtime",
        lambda: SimpleNamespace(health=health),
    )

    ready = await cli.readyz(_request())
    full = await cli.healthz(_request())

    assert ready.status_code == 200
    assert _payload(ready) == {"ok": True}
    assert full.status_code == 503
    assert _payload(full) == {"ok": False}
    assert health.readiness_calls == 1
    assert health.health_calls == 1
