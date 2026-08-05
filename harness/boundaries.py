"""
boundaries.py — the interlock logic, as a PURE function.

is_write_allowed() decides whether a file write is permitted given the current
phase's allowed_writes globs. It is deliberately free of any SDK dependency so it
can be unit-tested exhaustively with zero credits and zero network.

The Copilot SDK permission handler (wired in a later phase) is a thin adapter:
it extracts the path from the PermissionRequest and calls THIS function. All the
actual security logic lives here, where it is testable.
"""
from pathlib import PurePosixPath
import fnmatch


def _normalize(path: str) -> str:
    """Normalize a path to repo-relative POSIX form for matching."""
    p = path.replace("\\", "/")
    # strip leading ./ and any leading /
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    # collapse any .. by resolving against root (defensive against escapes)
    parts = []
    for seg in PurePosixPath(p).parts:
        if seg == "..":
            if parts:
                parts.pop()
            # if parts empty, an attempt to escape root -> keep as marker
            else:
                parts.append("..")
        elif seg == ".":
            continue
        else:
            parts.append(seg)
    return "/".join(parts)


def _matches(path: str, pattern: str) -> bool:
    """
    Glob match where '**' means 'any number of path segments'.
    'src/main/**' matches 'src/main/java/X.java' and 'src/main/'.
    """
    path = _normalize(path)
    pattern = pattern.replace("\\", "/").lstrip("/")

    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")

    # exact or single-level fnmatch fallback
    return fnmatch.fnmatch(path, pattern)


def _to_repo_relative(path: str, repo_root: str | None) -> str:
    """If `path` is absolute and under repo_root, return the repo-relative part.
    Otherwise return the normalized path unchanged."""
    p = path.replace("\\", "/")
    if repo_root:
        root = repo_root.replace("\\", "/").rstrip("/")
        # case-insensitive compare on Windows drive paths
        if p.lower().startswith(root.lower() + "/"):
            return p[len(root) + 1:]
        if p.lower() == root.lower():
            return ""
    return p


def is_excluded(path: str, exclude_globs, repo_root: str | None = None) -> bool:
    """
    Return True iff `path` matches any exclude glob — an ALWAYS-DENY set that
    wins over allowed_writes. Used for generated code that must never be edited
    even when it sits inside an otherwise-writable module (e.g. the -openapi-code
    module, generated model packages, MapStruct *MapperImpl.java).

    Empty/None exclude_globs => nothing excluded.
    """
    if not exclude_globs:
        return False
    rel = _to_repo_relative(path, repo_root)
    return any(_matches(rel, g) for g in exclude_globs)


def is_write_allowed(path: str, allowed_globs, repo_root: str | None = None,
                     exclude_globs=None) -> bool:
    """
    Return True iff `path` is permitted to be written under the current phase.

    - Absolute paths under repo_root are relativized first (the SDK reports
      absolute paths; our globs are repo-relative).
    - An escape attempt ('..' above root) is ALWAYS denied.
    - A path matching `exclude_globs` is ALWAYS denied — DENY WINS OVER ALLOW.
      This protects generated code that lives inside a writable module.
    - Empty allowed_globs => read-only phase => everything denied.
    """
    rel = _to_repo_relative(path, repo_root)
    norm = _normalize(rel)
    if norm.startswith(".."):
        return False
    # An absolute path that did NOT resolve under repo_root is out of bounds.
    if (rel.startswith("/") or (len(rel) > 1 and rel[1] == ":")):
        return False
    # DENY WINS: an excluded path is refused even if an allow-glob matches it.
    if is_excluded(rel, exclude_globs, repo_root):
        return False
    if not allowed_globs:
        return False
    return any(_matches(rel, g) for g in allowed_globs)


def deny_reason(path: str, phase_id: str, allowed_globs, repo_root: str | None = None,
                exclude_globs=None) -> str:
    """Human-readable explanation for an audit log when a write is denied."""
    rel = _normalize(_to_repo_relative(path, repo_root))
    if is_excluded(rel, exclude_globs, repo_root):
        return (
            f"BOUNDARY_VIOLATION in phase '{phase_id}': "
            f"write to '{rel}' is EXCLUDED (generated/protected path) "
            f"matching {list(exclude_globs or [])}"
        )
    return (
        f"BOUNDARY_VIOLATION in phase '{phase_id}': "
        f"write to '{rel}' is outside allowed paths {list(allowed_globs)}"
    )
