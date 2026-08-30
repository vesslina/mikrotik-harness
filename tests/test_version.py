import tomllib
from pathlib import Path

from mth import __version__


def test_runtime_version_matches_project_metadata() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text("utf-8"))

    assert __version__ == project["project"]["version"]
