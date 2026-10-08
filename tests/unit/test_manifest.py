"""The manifest has to identify the code that ran, and must not carry a token out with it.

``git status --porcelain | shasum -a 256`` hashed a list of filenames and status letters.
Two working trees whose uncommitted files differ in every single line produce the same
digest under it, and most of this project's implementation is currently uncommitted — so
it fails in exactly the situation the evidence needs it for.
"""

from __future__ import annotations

import json
from pathlib import Path

from event_arb.config import Settings
from event_arb.manifest import build_manifest


def tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_content_changes_change_the_digest(tmp_path: Path) -> None:
    """The whole point: same filenames, different contents, different digest."""
    tree(tmp_path, {"src/event_arb/thing.py": "value = 1\n"})
    first = build_manifest(Settings(), tmp_path)

    tree(tmp_path, {"src/event_arb/thing.py": "value = 2\n"})
    second = build_manifest(Settings(), tmp_path)

    assert first.overall_digest != second.overall_digest
    assert first.files.keys() == second.files.keys(), "the file list is identical"


def test_identical_content_gives_an_identical_digest(tmp_path: Path) -> None:
    tree(tmp_path, {"src/event_arb/thing.py": "value = 1\n", "tests/unit/t.py": "x = 1\n"})
    first = build_manifest(Settings(), tmp_path)
    second = build_manifest(Settings(), tmp_path)
    assert first.overall_digest == second.overall_digest


def test_untracked_source_files_are_hashed(tmp_path: Path) -> None:
    """Uncommitted implementation is still the implementation that produced the evidence."""
    tree(tmp_path, {"src/event_arb/brand_new.py": "# never committed\n"})
    manifest = build_manifest(Settings(), tmp_path)
    assert "src/event_arb/brand_new.py" in manifest.files


def test_a_secret_bearing_file_is_named_but_never_read(tmp_path: Path) -> None:
    """The manifest is written into evidence directories; a token must not ride along."""
    tree(
        tmp_path,
        {
            "config/trial.env": "EVENT_ARB_API_AUTH_TOKEN=super-secret-value\n",
            "config/trial.env.example": "EVENT_ARB_API_AUTH_TOKEN=\n",
            "src/event_arb/ok.py": "x = 1\n",
        },
    )
    manifest = build_manifest(Settings(), tmp_path)

    assert "config/trial.env" not in manifest.files
    assert "config/trial.env" in manifest.excluded_secret_files
    # The committed example holds no secret and its content matters, so it is hashed.
    assert "config/trial.env.example" in manifest.files

    serialized = manifest.to_json()
    assert "super-secret-value" not in serialized


def test_a_configured_token_is_never_serialized(tmp_path: Path) -> None:
    """Pydantic renders SecretStr as its type. Pinned, because it is load-bearing here."""
    tree(tmp_path, {"src/event_arb/ok.py": "x = 1\n"})
    settings = Settings(api_auth_token="live-trial-token")  # type: ignore[arg-type]
    manifest = build_manifest(settings, tmp_path)

    serialized = manifest.to_json()
    assert "live-trial-token" not in serialized
    assert manifest.settings["api_auth_token"] != "live-trial-token"


def test_build_output_and_local_state_are_not_hashed(tmp_path: Path) -> None:
    """A manifest that changed on every test run would identify nothing."""
    tree(
        tmp_path,
        {
            "src/event_arb/ok.py": "x = 1\n",
            "src/event_arb/__pycache__/ok.cpython-313.pyc": "compiled\n",
            "src/event_arb/trial.db": "sqlite\n",
            "src/event_arb/trial.db.journal": "{}\n",
        },
    )
    manifest = build_manifest(Settings(), tmp_path)

    assert list(manifest.files) == ["src/event_arb/ok.py"]


def test_the_manifest_round_trips_as_json(tmp_path: Path) -> None:
    tree(tmp_path, {"src/event_arb/ok.py": "x = 1\n"})
    manifest = build_manifest(Settings(), tmp_path)
    payload = json.loads(manifest.to_json())

    assert payload["overall_digest"] == manifest.overall_digest
    assert payload["file_count"] == 1
    assert payload["files"]["src/event_arb/ok.py"] == manifest.files["src/event_arb/ok.py"]


def test_a_renamed_file_changes_the_digest(tmp_path: Path) -> None:
    """Content alone is not identity: where a file lives is part of what ran."""
    tree(tmp_path, {"src/event_arb/a.py": "x = 1\n"})
    first = build_manifest(Settings(), tmp_path)

    (tmp_path / "src/event_arb/a.py").rename(tmp_path / "src/event_arb/b.py")
    second = build_manifest(Settings(), tmp_path)

    assert first.overall_digest != second.overall_digest
