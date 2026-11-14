"""Keep source version metadata aligned before building a release."""

from pathlib import Path
import tomllib

from pyneolink import __version__


def test_package_version_matches_public_sdk_version():
    """Packaging and runtime imports must report the same prepared version."""
    project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_path.open("rb") as source:
        project = tomllib.load(source)
    assert project["project"]["version"] == __version__


def test_release_has_matching_readme_and_changelog():
    """Release documentation must name the same version as the SDK."""
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"Version: `{__version__}` alpha." in readme
    assert f"## {__version__}" in changelog.splitlines()
