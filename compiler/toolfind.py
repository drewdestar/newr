"""Shared tool lookup for the compiler, its tests, and the
conformance runner.

On Windows, `shutil.which` builds candidates from PATHEXT, whose
entries are uppercase (`.COM;.EXE;.BAT;...`), and the filesystem
is case-insensitive, so `which("dart")` returns `...\\dart.EXE`
even though the file on disk is `dart.exe`. That spelling works
when the path is executed directly (CreateProcess resolves it),
but tools that derive a path from it break: Dart's build-hook
runner appends `.exe` itself and looks for `dart.EXE.exe`, which
does not exist (CI run 46053910369, dart-win:
`test_db_probe_runs`, `test_db_program_bundle_runs`,
`test_find_tool_uses_dart_sdk_dir`). Every toolchain lookup in
this repo goes through `which` here, so every caller gets the
spelling the directory actually holds.

The conformance runner (`conformance/run.py`) is a standalone
script that cannot import this package, so it carries its own
copy of `on_disk_spelling` (same rule, noted there).
"""

import os
import shutil


def on_disk_spelling(path: str) -> str:
    """The path with the spelling the directory actually holds.

    An exact match is returned unchanged (the common case on
    Linux, where `which` already returns the real name). Otherwise
    the first entry that matches case-insensitively wins. A path
    whose file or directory does not exist is returned unchanged.
    """
    directory, name = os.path.split(path)
    if not name:
        return path
    try:
        entries = os.listdir(directory or ".")
    except OSError:
        return path
    if name in entries:
        return path
    lowered = name.lower()
    for entry in entries:
        if entry.lower() == lowered:
            return os.path.join(directory, entry) if directory \
                else entry
    return path


def which(name: str, path: str | None = None) -> str | None:
    """`shutil.which`, with the result spelled as it is on disk
    (see the module docstring). None when the tool is nowhere to
    be found."""
    found = shutil.which(name, path=path)
    if found is None:
        return None
    return on_disk_spelling(found)
