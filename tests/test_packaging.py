import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_runtime_resource_files_are_declared_as_package_data():
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["rumi_helper"]

    packaged = {
        path.relative_to(root / "rumi_helper").as_posix()
        for pattern in patterns
        for path in (root / "rumi_helper").glob(pattern)
    }
    required = {
        "personality/rumi.yaml",
        "personality/luna.yaml",
        "prompts/agent.txt",
        "prompts/chat.txt",
        "prompts/webpage_summary.txt",
    }

    assert required <= packaged


def test_wheel_declares_packaged_console_entrypoints_and_socks_dependency():
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())

    assert config["project"]["scripts"]["rumi-helper"] == "rumi_helper.cli:main"
    assert config["project"]["scripts"]["rumi-doctor"] == "rumi_helper.doctor:main"
    assert config["tool"]["setuptools"]["packages"]["find"]["include"] == ["rumi_helper*"]
    assert "httpx[socks]>=0.27.0" in config["project"]["dependencies"]


def test_packaged_cli_loads_from_outside_project_cwd(tmp_path):
    root = Path(__file__).parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import rumi_helper.cli as cli; "
                "paths={route.path for route in cli.driver.server_app.routes}; "
                "assert {'/livez','/readyz','/healthz'} <= paths"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
