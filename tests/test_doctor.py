import os
import socket

import pytest

from rumi_helper.config import Settings
from rumi_helper.doctor import main, run_checks


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _settings(tmp_path, **overrides):
    values = {
        "_env_file": None,
        "host": "127.0.0.1",
        "port": _free_port(),
        "database_path": str(tmp_path / "data" / "bot.db"),
        "scheduler_timezone": "UTC",
        "github_check_cron": "",
        "github_digest_cron": "",
        "daily_report_cron": "",
    }
    values.update(overrides)
    return Settings(**values)


def _statuses(checks):
    return {check.name: check.status for check in checks}


def test_doctor_accepts_safe_local_configuration_with_warnings(tmp_path):
    checks = run_checks(_settings(tmp_path))
    statuses = _statuses(checks)

    assert "FAIL" not in statuses.values()
    assert statuses["bind_available"] == "PASS"
    assert statuses["onebot_token"] == "WARN"
    assert statuses["llm_api_key"] == "WARN"
    assert statuses["llm_model"] == "PASS"
    assert statuses["onebot_exposure"] == "WARN"
    assert statuses["llm_transport"] == "PASS"
    assert statuses["admin_ids"] == "WARN"
    assert statuses["owner_id"] == "WARN"


def test_doctor_rejects_invalid_core_configuration(tmp_path):
    settings = _settings(
        tmp_path,
        port=0,
        llm_base_url="file:///tmp/model",
        admin_qq_ids="not-a-qq",
        owner_qq_id="also-bad",
        database_path=str(tmp_path),
        llm_timeout=0,
    )
    statuses = _statuses(run_checks(settings))

    assert statuses["bind"] == "FAIL"
    assert statuses["llm_url"] == "FAIL"
    assert statuses["admin_ids"] == "FAIL"
    assert statuses["owner_id"] == "FAIL"
    assert statuses["database_path"] == "FAIL"
    assert statuses["numeric_limits"] == "FAIL"


def test_doctor_rejects_invalid_timezone(tmp_path):
    statuses = _statuses(
        run_checks(_settings(tmp_path, scheduler_timezone="Not/A-Timezone"))
    )
    assert statuses["timezone"] == "FAIL"


def test_doctor_rejects_invalid_cron(tmp_path):
    statuses = _statuses(
        run_checks(_settings(tmp_path, github_check_cron="not a cron"))
    )
    assert statuses["github_check_cron"] == "FAIL"


def test_doctor_does_not_echo_secret_when_settings_validation_fails(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PORT", "not-an-int")
    monkeypatch.setenv("LLM_API_KEY", "super-secret-do-not-print")

    assert main() == 1

    output = capsys.readouterr().out
    assert "CHECK settings FAIL" in output
    assert "super-secret-do-not-print" not in output


def test_doctor_database_path_checks_existing_file_permissions(tmp_path, monkeypatch):
    database = tmp_path / "bot.db"
    database.write_bytes(b"")
    monkeypatch.setattr(os, "access", lambda path, mode: path != database)

    statuses = _statuses(
        run_checks(_settings(tmp_path, database_path=str(database)))
    )
    assert statuses["database_path"] == "FAIL"

def test_doctor_rejects_unauthenticated_non_loopback_bind(tmp_path):
    statuses = _statuses(
        run_checks(
            _settings(
                tmp_path,
                host="0.0.0.0",
                onebot_access_token="",
            )
        )
    )
    assert statuses["onebot_exposure"] == "FAIL"


def test_doctor_allows_authenticated_non_loopback_bind(tmp_path):
    statuses = _statuses(
        run_checks(
            _settings(
                tmp_path,
                host="0.0.0.0",
                onebot_access_token="shared-secret",
            )
        )
    )
    assert statuses["onebot_exposure"] == "PASS"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@llm.example.com/v1",
        "https://llm.example.com/v1?token=secret",
        "https://llm.example.com/v1#fragment",
    ],
)
def test_doctor_rejects_sensitive_or_ambiguous_llm_base_urls(tmp_path, url):
    statuses = _statuses(
        run_checks(
            _settings(
                tmp_path,
                llm_base_url=url,
            )
        )
    )
    assert statuses["llm_url"] == "FAIL"


def test_doctor_rejects_remote_plain_http_llm_with_api_key(tmp_path):
    statuses = _statuses(
        run_checks(
            _settings(
                tmp_path,
                llm_base_url="http://llm.example.com/v1",
                llm_api_key="secret",
            )
        )
    )
    assert statuses["llm_transport"] == "FAIL"


def test_doctor_allows_loopback_http_llm_with_api_key(tmp_path):
    statuses = _statuses(
        run_checks(
            _settings(
                tmp_path,
                llm_base_url="http://127.0.0.1:8000/v1",
                llm_api_key="secret",
            )
        )
    )
    assert statuses["llm_transport"] == "PASS"


def test_doctor_rejects_occupied_bind_port(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        statuses = _statuses(
            run_checks(
                _settings(
                    tmp_path,
                    host="127.0.0.1",
                    port=port,
                )
            )
        )

    assert statuses["bind_available"] == "FAIL"


def test_doctor_rejects_http_web_proxy(tmp_path):
    checks = run_checks(
        _settings(tmp_path, web_proxy_url="http://127.0.0.1:7897")
    )
    statuses = _statuses(checks)
    assert statuses["web_proxy"] == "FAIL"


def test_doctor_rejects_web_proxy_with_path(tmp_path):
    checks = run_checks(
        _settings(tmp_path, web_proxy_url="socks5://127.0.0.1:7897/not-valid")
    )
    statuses = _statuses(checks)
    assert statuses["web_proxy"] == "FAIL"


def test_doctor_accepts_socks5_web_proxy_without_echoing_credentials(tmp_path):
    checks = run_checks(
        _settings(
            tmp_path,
            web_proxy_url="socks5://user:super-secret@127.0.0.1:7897",
        )
    )
    status = next(check for check in checks if check.name == "web_proxy")
    assert status.status == "PASS"
    assert "super-secret" not in status.detail


def test_doctor_warns_when_playwright_enabled(tmp_path):
    checks = run_checks(_settings(tmp_path, enable_playwright=True))
    statuses = _statuses(checks)
    assert statuses["playwright_ssrf"] == "WARN"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("send_rate_limit_per_second", 0),
        ("interactive_send_rate_limit_per_second", -1),
        ("outbound_retry_delay_seconds", 0),
        ("outbound_account_pause_seconds", -1),
        ("outbound_rate_limit_pause_seconds", 0),
        ("outbound_queue_poll_seconds", 0),
        ("max_active_reminders_per_user", 0),
        ("max_github_repositories_per_user", -1),
        ("max_context_messages", 0),
        ("max_context_chars", 0),
    ],
)
def test_doctor_rejects_disabled_or_invalid_protective_limits(
    tmp_path,
    field,
    value,
):
    statuses = _statuses(run_checks(_settings(tmp_path, **{field: value})))
    assert statuses["numeric_limits"] == "FAIL"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("web_cache_ttl_seconds", -1),
        ("outbound_terminal_history_limit", -1),
        ("send_log_history_limit", -1),
        ("non_llm_reply_delay_min_seconds", -0.1),
        ("non_llm_reply_delay_max_seconds", -0.1),
    ],
)
def test_doctor_rejects_negative_nonnegative_limits(tmp_path, field, value):
    statuses = _statuses(run_checks(_settings(tmp_path, **{field: value})))
    assert statuses["numeric_limits"] == "FAIL"


def test_doctor_allows_documented_zero_disable_values(tmp_path):
    statuses = _statuses(
        run_checks(
            _settings(
                tmp_path,
                web_cache_ttl_seconds=0,
                outbound_terminal_history_limit=0,
                send_log_history_limit=0,
            )
        )
    )
    assert statuses["numeric_limits"] == "PASS"


def test_doctor_rejects_inverted_non_llm_reply_delay_range(tmp_path):
    statuses = _statuses(
        run_checks(
            _settings(
                tmp_path,
                non_llm_reply_delay_min_seconds=10,
                non_llm_reply_delay_max_seconds=2,
            )
        )
    )
    assert statuses["numeric_limits"] == "FAIL"


def test_doctor_rejects_blank_llm_model(tmp_path):
    statuses = _statuses(run_checks(_settings(tmp_path, llm_model="   ")))

    assert statuses["llm_model"] == "FAIL"
