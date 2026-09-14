"""Install the repository-owned operating skill in the user's global skill directory."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

SOURCE = Path(__file__).resolve().parents[1] / "skills" / "plane-mcp-operations"


def manifest(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Skill trees must not contain symlinks")
        if path.is_file():
            if path.suffix != ".md":
                raise ValueError("Unexpected non-document skill resource")
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "SKILL.md" not in result:
        raise ValueError("Missing SKILL.md")
    return result


def install(source: Path, destination: Path, *, apply: bool) -> dict:
    expected = manifest(source)
    if destination.is_symlink():
        raise ValueError("Refusing to replace a symlink destination")
    current = manifest(destination) if destination.exists() else {}
    result = {"source": str(source), "destination": str(destination),
              "files": len(expected), "in_sync": expected == current, "applied": False}
    if not apply or result["in_sync"]:
        return result
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if destination.exists():
        backup_parent = destination.parent.parent / ".skill-backups"
        backup_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup_root = Path(tempfile.mkdtemp(prefix="plane-skill-backup-", dir=backup_parent))
        backup_root.chmod(0o700)
        backup = backup_root / destination.name
        shutil.copytree(destination, backup)
        assert manifest(backup) == current
        result["backup"] = str(backup)
    staging = Path(tempfile.mkdtemp(prefix=".plane-skill-stage-", dir=destination.parent))
    try:
        shutil.copytree(source, staging / destination.name)
        assert manifest(staging / destination.name) == expected
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(staging / destination.name), str(destination))
    except Exception:
        if backup is not None and not destination.exists():
            shutil.copytree(backup, destination)
        raise
    finally:
        shutil.rmtree(staging)
    assert manifest(destination) == expected
    result.update(in_sync=True, applied=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = install(SOURCE, Path.home() / ".agents/skills/plane-mcp-operations", apply=args.apply)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
