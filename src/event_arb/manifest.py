"""A content hash of the code and configuration a run was produced by.

The runbook used to identify the build with ``git status --porcelain | shasum -a 256``.
That hashes a list of *filenames and status letters*, so it is identical for two working
trees whose uncommitted files differ in every line, and it changes when a file is merely
renamed. Most of this project's implementation is currently uncommitted, which makes that
the one situation the check cannot survive: the evidence would name a revision that does
not describe the code that produced it.

So this reads the files and hashes their contents. Every source file that can affect a
run's behaviour is included whether or not git tracks it, each with its own digest, plus
one overall digest over the sorted ``(path, digest)`` pairs. Two trees with the same
overall digest ran the same code; two with different ones did not, and the per-file list
says where they diverged.

**Secrets are never read.** The file an operator is told to put an API token in is
excluded by name, and so is anything else matching a secret-shaped pattern — the manifest
is written into evidence directories and attached to reports, which is exactly the path by
which a token would escape. The effective configuration is included, but through
Pydantic's ``SecretStr`` rendering, so a token appears as its type and never its value.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from event_arb.config import Settings

__all__ = ["Manifest", "build_manifest"]

#: Directories walked for source content. A run's behaviour comes from these.
#:
#: ``config`` is walked even though its only committed member is an example file. Walking
#: it is what makes the secret exclusion *fire* and get named, so the manifest can state
#: that it saw a credential file and did not read it — rather than silently never looking.
SOURCE_ROOTS = ("src", "tests", "alembic", "config")

#: Individually named files that also change behaviour.
SOURCE_FILES = (
    "pyproject.toml",
    "alembic.ini",
    "mypy-tests.ini",
    "config/trial.env.example",
)

#: Never read, never hashed, never named. Ordered from most to least obvious.
SECRET_PATTERNS = (
    ".env",
    ".key",
    ".pem",
    ".p8",
    ".pkcs8",
    ".keystore",
    ".jks",
    "secret",
    "credential",
    "token",
    "password",
    "wallet",
    "id_rsa",
)

#: Skipped because they are build output or local state, not source.
SKIP_DIRECTORIES = frozenset(
    {
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".git",
        "node_modules",
        "htmlcov",
    }
)

SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".journal", ".log"})


def _is_secret(path: Path) -> bool:
    """Whether this path looks like it holds a credential.

    Deliberately over-broad, and matched against the whole relative path rather than the
    basename: excluding a source file by mistake produces a manifest that is missing a
    file, which the per-file listing makes visible. Including a token produces evidence
    that leaks it, which nothing downstream makes visible at all. The two errors are not
    symmetric, so this errs toward exclusion.
    """
    text = str(path).lower()
    if text.endswith(".env.example") or text.endswith(".env.sample"):
        # An example file is committed precisely because it holds no secret, and leaving
        # it out would hide a change to the configuration an operator is told to copy.
        return False
    return any(pattern in text for pattern in SECRET_PATTERNS)


@dataclass
class Manifest:
    """Content digests for one working tree, plus the configuration it would run with."""

    generated_at: str = ""
    overall_digest: str = ""
    files: dict[str, str] = field(default_factory=dict)
    excluded_secret_files: list[str] = field(default_factory=list)
    git_commit: str = ""
    git_is_dirty: bool = False
    untracked_source_files: list[str] = field(default_factory=list)
    settings: dict[str, object] = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.files)

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "overall_digest": self.overall_digest,
            "file_count": self.file_count,
            "git_commit": self.git_commit,
            "git_is_dirty": self.git_is_dirty,
            "untracked_source_files": list(self.untracked_source_files),
            "excluded_secret_files": list(self.excluded_secret_files),
            "files": dict(self.files),
            "settings": dict(self.settings),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True) + "\n"

    def render(self) -> str:
        lines = [
            "Revision manifest",
            f"  generated_at   : {self.generated_at}",
            f"  overall digest : {self.overall_digest}",
            f"  files hashed   : {self.file_count}",
            f"  git commit     : {self.git_commit or 'unknown'}"
            + ("  (working tree is dirty)" if self.git_is_dirty else ""),
        ]
        if self.untracked_source_files:
            lines.append(
                f"  untracked      : {len(self.untracked_source_files)} source file(s) not "
                "in git, hashed by content"
            )
        if self.excluded_secret_files:
            lines.append(
                f"  excluded       : {len(self.excluded_secret_files)} secret-shaped "
                "file(s), named but never read"
            )
        return "\n".join(lines)


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _git(root: Path, *args: str) -> str:
    """Run one git command, returning empty string when git cannot answer.

    A manifest must still be produced outside a checkout — the content digests are the
    part that matters, and the commit is context.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _candidate_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for name in SOURCE_ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRECTORIES for part in path.parts):
                continue
            if path.suffix in SKIP_SUFFIXES:
                continue
            found.add(path)
    for name in SOURCE_FILES:
        path = root / name
        if path.is_file():
            found.add(path)
    return sorted(found)


def build_manifest(settings: Settings, root: Path | None = None) -> Manifest:
    """Hash every source file's content, tracked or not, and record the configuration."""
    base = (root or Path.cwd()).resolve()
    manifest = Manifest(generated_at=datetime.now(UTC).isoformat())

    tracked = {line.strip() for line in _git(base, "ls-files").splitlines() if line.strip()}

    for path in sorted(_candidate_files(base)):
        relative = path.relative_to(base).as_posix()
        if _is_secret(path.relative_to(base)):
            # Named so the manifest is honest about what it did not cover, and so a
            # reviewer can see that the exclusion fired rather than wondering.
            manifest.excluded_secret_files.append(relative)
            continue
        manifest.files[relative] = _digest(path)
        if tracked and relative not in tracked:
            manifest.untracked_source_files.append(relative)

    combined = hashlib.sha256()
    for relative, digest in sorted(manifest.files.items()):
        combined.update(f"{relative}\0{digest}\0".encode())
    manifest.overall_digest = combined.hexdigest()

    manifest.git_commit = _git(base, "rev-parse", "HEAD")
    manifest.git_is_dirty = bool(_git(base, "status", "--porcelain"))
    # Pydantic renders SecretStr as its type, never its value, so this cannot carry a
    # token into an evidence directory.
    manifest.settings = json.loads(settings.model_dump_json())
    return manifest
