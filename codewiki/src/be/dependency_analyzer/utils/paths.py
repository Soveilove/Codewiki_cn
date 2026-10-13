"""Path helpers shared by the language analyzers.

Component ids and dependency-graph paths must be identical on every OS:
``src/lib.rs::App`` on macOS and Windows alike. ``os.path.relpath``
returns ``\\`` on Windows, so every analyzer funnels its relative paths
through :func:`posix_relpath`, which always yields ``/``.
"""

from __future__ import annotations

import os


def posix_relpath(
    file_path: str | os.PathLike[str], repo_path: str | os.PathLike[str] | None
) -> str:
    """Relative path from the repo root, always with ``/`` separators.

    Falls back to the absolute path (still normalised) when the file sits
    outside ``repo_path`` — on Windows that is a different drive, where
    ``os.path.relpath`` raises ``ValueError``.
    """
    if repo_path:
        try:
            return os.path.relpath(str(file_path), str(repo_path)).replace(os.sep, "/")
        except ValueError:
            return str(file_path).replace(os.sep, "/")
    return str(file_path).replace(os.sep, "/")
