from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_qqbot_systemd_service_keeps_preflight_and_sandbox():
    text = (ROOT / "ops" / "qqbot.service").read_text()

    required = {
        "User=qqbot",
        "Group=qqbot",
        "WorkingDirectory=/opt/RumiHelper",
        "ExecStartPre=/opt/RumiHelper/.venv/bin/rumi-doctor",
        "ExecStart=/opt/RumiHelper/.venv/bin/rumi-helper",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "RestrictSUIDSGID=true",
        "LockPersonality=true",
        "ReadWritePaths=/opt/RumiHelper/data",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "Environment=PYTHONDONTWRITEBYTECODE=1",
        "UMask=0077",
    }

    missing = sorted(line for line in required if line not in text)
    assert missing == []


def test_qqbot_systemd_service_uses_graceful_restart_policy():
    text = (ROOT / "ops" / "qqbot.service").read_text()

    assert "Restart=on-failure" in text
    assert "KillSignal=SIGTERM" in text
    assert "TimeoutStopSec=45" in text
    assert "User=root" not in text


def test_napcat_watchdog_units_keep_bounded_periodic_execution():
    service = (ROOT / "ops" / "napcat" / "napcat-watchdog.service").read_text()
    timer = (ROOT / "ops" / "napcat" / "napcat-watchdog.timer").read_text()

    assert "Type=oneshot" in service
    assert "EnvironmentFile=-/etc/napcat/napcat.env" in service
    assert "ExecStart=/usr/local/sbin/napcat-watchdog" in service
    assert "TimeoutStartSec=90" in service
    assert "UMask=0077" in service

    assert "OnBootSec=2min" in timer
    assert "OnUnitActiveSec=1min" in timer
    assert "Unit=napcat-watchdog.service" in timer
