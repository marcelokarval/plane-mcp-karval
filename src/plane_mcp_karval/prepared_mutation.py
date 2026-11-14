"""Read one integrity-bound operation from the local prepared-mutations directory."""

import hashlib
import hmac
import json
import os
import re
import stat
from typing import Any

MAX_BYTES = 8 * 1024 * 1024
MESSAGE = "Use plane_mutation_action with a bound prepared operation file and SHA256"


def reject_unbound_html(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"description_html", "comment_html"}:
                raise ValueError(MESSAGE)
            reject_unbound_html(item)
    elif isinstance(value, list):
        for item in value:
            reject_unbound_html(item)


def load_prepared(filename: str, digest: str) -> dict[str, Any]:
    """Open a basename only, without following links, and verify one snapshot."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid prepared operation binding")
    root = directory = fd = None
    try:
        root = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directory = os.open(".prepared-mutations", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
            raise ValueError("Invalid prepared operation file")
        chunks = []
        remaining = before.st_size + 1
        while remaining:
            part = os.read(fd, min(65536, remaining))
            if not part:
                break
            chunks.append(part)
            remaining -= len(part)
        data = b"".join(chunks)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size:
            raise ValueError("Prepared operation changed during read")
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
            raise ValueError("Prepared operation integrity mismatch")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate prepared operation key")
                result[key] = value
            return result
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON")))
        if not isinstance(value, dict) or set(value) != {"operation", "path_params", "query", "payload", "idempotency_key"} or not isinstance(value["path_params"], dict) or not isinstance(value["query"], (dict, type(None))) or not isinstance(value["payload"], (dict, type(None))):
            raise ValueError("Invalid prepared operation structure")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Invalid prepared operation file") from None
    finally:
        for item in (fd, directory, root):
            if item is not None:
                os.close(item)
