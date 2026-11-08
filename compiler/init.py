"""`newr init` and `newr.json` support (SPEC.md section 11, M6 Phase 2b).

A project is a directory with a `newr.json` manifest plus a `main.newr`
program. `newr init` scaffolds one:

    main.newr      hello program (the project's entry point)
    newr.json      manifest: name, version, target, default_locale
    locales/       locale files live here one day; v1 strings are inline
                   `strings` blocks (SPEC section 15), so the directory
                   ships empty except for a `.gitkeep`
    .env.example   secrets template (the token stays in `.env`, never in
                   newr.json or in source)
    .gitignore     the usual ignores for a newr project

The manifest schema is SPEC section 11:

    { "name": str, "version": str, "target": "py" | "js" | ...,
      "default_locale": str, "db_path": str }

`name` and `version` are required; `target`, `default_locale`, and
`db_path` are optional. `db_path` is schema-only in v1: nothing reads
it yet (the store's database path is decided by the runtime), so the
scaffold omits it rather than writing a value nothing honours.

`newr build` without `--target` reads the manifest for the default
target: it searches for `newr.json` starting in the main file's
directory and walking up. An explicit `--target` always wins. A
missing manifest changes nothing (the default stays `py`, as before);
a malformed manifest, or a `target` value that is not one of the
seven targets, is an error naming the file, never a silent fallback.

`newr init` refuses to run in a directory that already has a
`newr.json` or a `main.newr`, so it can never clobber existing work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

VALID_TARGETS = ("py", "js", "java", "cs", "rs", "kt", "dart")

MANIFEST_FILENAME = "newr.json"

PROJECT_VERSION = "0.1.0"

DEFAULT_LOCALE = "en"

# How to run the scaffolded program once built, per target. Plain
# programs only: a bot program also gets deploy/registration steps
# (see `newr help build`).
_RUN_COMMAND = {
    "py": "python main.py",
    "js": "node main.js",
    "java": "javac *.java && java Main",
    "cs": "dotnet run --project main.csproj",
    "rs": "rustc main.rs && ./main",
    "kt": "kotlinc *.kt -include-runtime -d main.jar && java -jar main.jar",
    "dart": "dart main.dart",
}


class ManifestError(ValueError):
    """The manifest exists but cannot be used (bad JSON or bad schema)."""


class InitError(ValueError):
    """`newr init` cannot run here (bad target, or files exist)."""


@dataclass(frozen=True)
class Manifest:
    """A parsed `newr.json` (SPEC.md section 11)."""

    name: str
    version: str
    target: str | None = None
    default_locale: str | None = None
    db_path: str | None = None

    def to_dict(self) -> dict:
        """The manifest as a JSON object (omits absent optionals)."""
        data: dict = {"name": self.name, "version": self.version}
        if self.target is not None:
            data["target"] = self.target
        if self.default_locale is not None:
            data["default_locale"] = self.default_locale
        if self.db_path is not None:
            data["db_path"] = self.db_path
        return data


def load_manifest(path: Path) -> Manifest:
    """Read and validate a `newr.json` file.

    Raises ManifestError naming the file when the JSON is malformed,
    when the top level is not an object, when `name` or `version` is
    missing or not a string, when `target` is not one of the seven
    targets, or when `default_locale` / `db_path` is not a string.
    Unknown keys are ignored (forward compatibility).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(
            f"{path} must contain a JSON object, not "
            f"{type(data).__name__}")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(
            f"{path}: `name` is required and must be a non-empty "
            f"string")
    version = data.get("version")
    if not isinstance(version, str) or not version:
        raise ManifestError(
            f"{path}: `version` is required and must be a non-empty "
            f"string")
    target = data.get("target")
    if target is not None:
        if not isinstance(target, str) or target not in VALID_TARGETS:
            raise ManifestError(
                f"{path}: `target` must be one of "
                f"{', '.join(VALID_TARGETS)} (got {target!r})")
    default_locale = data.get("default_locale")
    if default_locale is not None and not isinstance(default_locale, str):
        raise ManifestError(
            f"{path}: `default_locale` must be a string "
            f"(got {default_locale!r})")
    db_path = data.get("db_path")
    if db_path is not None and not isinstance(db_path, str):
        raise ManifestError(
            f"{path}: `db_path` must be a string (got {db_path!r})")
    return Manifest(name=name, version=version, target=target,
                    default_locale=default_locale, db_path=db_path)


def find_manifest(start: Path) -> Path | None:
    """Find the nearest `newr.json` at or above `start`.

    `start` may be a file (its parent directory is used) or a
    directory. Walks up to the filesystem root; returns None when no
    manifest exists anywhere above.
    """
    directory = start if start.is_dir() else start.parent
    directory = directory.resolve()
    while True:
        candidate = directory / MANIFEST_FILENAME
        if candidate.is_file():
            return candidate
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent


def default_target_for(main_path: Path) -> str:
    """The build target for a program: its manifest's `target`, or
    `py` when there is no manifest or the manifest sets no target.

    Raises ManifestError when a manifest is found but unusable.
    """
    manifest_path = find_manifest(Path(main_path))
    if manifest_path is None:
        return "py"
    manifest = load_manifest(manifest_path)
    return manifest.target or "py"


def main_source(name: str, target: str) -> str:
    """The scaffolded `main.newr` for a project."""
    run = _RUN_COMMAND[target]
    return (
        f"# {name}, a program written in the newr language.\n"
        f"# Build it with: newr build main.newr\n"
        f"# Then run it with: {run}\n"
        "\n"
        "print \"Hello, world!\"\n"
    )


def manifest_source(name: str, target: str) -> str:
    """The scaffolded `newr.json` for a project."""
    manifest = Manifest(name=name, version=PROJECT_VERSION,
                        target=target, default_locale=DEFAULT_LOCALE)
    return json.dumps(manifest.to_dict(), indent=2) + "\n"


ENV_EXAMPLE = """\
# Copy this file to .env and fill in the values.
# Only bot programs need it (DISCORD_TOKEN for a Discord bot).
# Never commit .env: it holds secrets. Secrets never go in newr.json.
DISCORD_TOKEN=
"""

GITIGNORE = """\
# newr project ignores
.env
__pycache__/
*.pyc
node_modules/
target/
dist/
*.db
.DS_Store
"""


def scaffold(directory: Path, *, name: str | None = None,
             target: str = "py") -> list[Path]:
    """Create a newr project in `directory` (made if missing).

    Returns the created files, in creation order. Raises InitError
    when `target` is unknown, or when `newr.json` or `main.newr`
    already exists in the directory. Raises OSError when a file
    cannot be written.
    """
    if target not in VALID_TARGETS:
        raise InitError(
            f"unknown target `{target}` (targets: "
            f"{', '.join(VALID_TARGETS)})")
    directory.mkdir(parents=True, exist_ok=True)
    resolved_name = name or directory.resolve().name or "app"
    if not resolved_name.strip():
        resolved_name = "app"
    for existing in (MANIFEST_FILENAME, "main.newr"):
        if (directory / existing).exists():
            raise InitError(
                f"{directory / existing} already exists; refusing "
                f"to overwrite it")
    files = {
        Path("main.newr"): main_source(resolved_name, target),
        Path(MANIFEST_FILENAME): manifest_source(resolved_name, target),
        Path("locales") / ".gitkeep": "",
        Path(".env.example"): ENV_EXAMPLE,
        Path(".gitignore"): GITIGNORE,
    }
    created: list[Path] = []
    for relative, text in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        created.append(path)
    return created
