"""Filesystem helpers: whitelist/blacklist path policy, read-only walking, hashing.

Hard guarantees:
- iter_files() only ever READS. Nothing in this module writes or deletes user files.
- Files under the service's own data/logs directories are never indexed
  (prevents the index from indexing itself).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Callable, Iterator


def is_under(path: str, root: str) -> bool:
    """True if `path` equals or is inside `root` (both absolute, realpath-normalized)."""
    try:
        pr = os.path.realpath(root)
        c = os.path.realpath(path)
    except OSError:
        return False
    if c == pr:
        return True
    return c.startswith(pr + os.sep)


def matches_exclude(name: str, patterns: list[str]) -> bool:
    for pat in patterns or []:
        if not pat:
            continue
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def path_in_list(path: str, roots: list[str]) -> bool:
    return any(is_under(path, r) for r in roots or [])


def has_indexable_ext(path: str, indexable_exts: set[str]) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in indexable_exts


def fingerprint(path: str, max_mb: int) -> dict:
    """Content fingerprint used for incremental updates: size + mtime_ns + sha256."""
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "sha256": h.hexdigest(),
    }


def iter_files(whitelist: list[str], blacklist: list[str], exclude_names: list[str],
               indexable_exts: set[str], max_file_mb: int,
               protected_dirs: list[str],
               on_error: Callable[[str, str], None] | None = None) -> Iterator[str]:
    """Yield indexable file paths strictly inside whitelist, minus blacklist.

    - Symlinks that resolve outside every whitelist root are skipped (cannot be
      used to escape the policy).
    - Protected dirs (service data/logs) are always skipped.
    - Read errors are reported via on_error(path, message) and skipped.
    """
    seen_roots = set()
    for root in whitelist or []:
        rp = os.path.realpath(root)
        if rp in seen_roots:
            continue
        seen_roots.add(rp)
        if not os.path.isdir(root):
            if on_error:
                on_error(root, "whitelist directory does not exist")
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # prune directories in place
            keep: list[str] = []
            for d in list(dirnames):
                full = os.path.join(dirpath, d)
                if d.startswith(".") and d not in (".", ".."):
                    continue  # hidden dirs skipped by default
                if matches_exclude(d, exclude_names):
                    continue
                if path_in_list(full, blacklist):
                    continue
                if path_in_list(full, protected_dirs):
                    continue
                if os.path.islink(full):
                    real = os.path.realpath(full)
                    if not any(is_under(real, r) for r in whitelist):
                        continue
                keep.append(d)
            dirnames[:] = keep
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                if path_in_list(full, blacklist):
                    continue
                if path_in_list(full, protected_dirs):
                    continue
                if matches_exclude(fn, exclude_names):
                    continue
                if not has_indexable_ext(full, indexable_exts):
                    continue
                if os.path.islink(full):
                    real = os.path.realpath(full)
                    if not any(is_under(real, r) for r in whitelist):
                        continue
                    full = real
                if not os.path.isfile(full):
                    continue
                try:
                    if os.path.getsize(full) > max_file_mb * 1024 * 1024:
                        if on_error:
                            on_error(full, f"file larger than max_file_mb={max_file_mb}, skipped")
                        continue
                except OSError as e:
                    if on_error:
                        on_error(full, f"stat failed: {e}")
                    continue
                yield full
