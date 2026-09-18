import os
import subprocess
import sys
import tomllib
from pathlib import Path


def test_runtime_resource_files_are_declared_as_package_data():
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    patterns = config["tool"]["setuptools"]["package-data"]["app"]

    packaged = {
        path.relative_to(root / "app").as_posix()
        for pattern in patterns
        for path in (root / "app").glob(pattern)
    }
    required = {
        "personality/rumi.yaml",
        "personality/luna.yaml",
        "prompts/agent.txt",
        "prompts/chat.txt",
        "prompts/webpage_summary.txt",
    }

    assert required <= packaged


def test_wheel_declares_console_entrypoint_and_root_bot_module():
    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())

    assert config["project"]["scripts"]["rumi-helper"] == "bot:main"
    assert config["project"]["scripts"]["rumi-doctor"] == "app.doctor:main"
    assert "bot" in config["tool"]["setuptools"]["py-modules"]


def test_bot_module_loads_from_outside_project_cwd(tmp_path):
    root = Path(__file__).parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import bot; "
                "paths={route.path for route in bot.driver.server_app.routes}; "
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
