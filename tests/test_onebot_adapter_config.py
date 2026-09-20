import os
import subprocess
import sys


def _probe_adapter_token(tmp_path, *, legacy: str, canonical: str, expected: str):
    env = os.environ.copy()
    env.pop("ONEBOT_ACCESS_TOKEN", None)
    env.pop("ONEBOT_V11_ACCESS_TOKEN", None)
    env["DRIVER"] = "~fastapi"
    if legacy:
        env["ONEBOT_ACCESS_TOKEN"] = legacy
    if canonical:
        env["ONEBOT_V11_ACCESS_TOKEN"] = canonical
    env["EXPECTED_ONEBOT_TOKEN"] = expected

    code = """
import os
import rumi_helper.cli
from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11.config import Config

actual = get_plugin_config(Config).onebot_access_token
expected = os.environ["EXPECTED_ONEBOT_TOKEN"]
raise SystemExit(0 if actual == expected else 7)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"adapter token bridge failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )


def test_legacy_onebot_token_is_bridged_to_adapter_canonical_key(tmp_path):
    _probe_adapter_token(
        tmp_path,
        legacy="legacy-test-token",
        canonical="",
        expected="legacy-test-token",
    )


def test_matching_canonical_and_legacy_onebot_tokens_are_accepted(tmp_path):
    _probe_adapter_token(
        tmp_path,
        legacy="same-test-token",
        canonical="same-test-token",
        expected="same-test-token",
    )


def test_conflicting_onebot_token_aliases_fail_before_adapter_init(tmp_path):
    canonical = "canonical-secret-do-not-print"
    legacy = "legacy-secret-do-not-print"
    env = os.environ.copy()
    env.pop("ONEBOT_ACCESS_TOKEN", None)
    env.pop("ONEBOT_V11_ACCESS_TOKEN", None)
    env["DRIVER"] = "~fastapi"
    env["ONEBOT_ACCESS_TOKEN"] = legacy
    env["ONEBOT_V11_ACCESS_TOKEN"] = canonical

    result = subprocess.run(
        [sys.executable, "-c", "import rumi_helper.cli"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "值不一致" in output
    assert canonical not in output
    assert legacy not in output


def test_legacy_dotenv_token_is_bridged_for_existing_deployments(tmp_path):
    (tmp_path / ".env").write_text(
        "ONEBOT_ACCESS_TOKEN=dotenv-legacy-token\n",
        encoding="utf-8",
    )

    _probe_adapter_token(
        tmp_path,
        legacy="",
        canonical="",
        expected="dotenv-legacy-token",
    )
