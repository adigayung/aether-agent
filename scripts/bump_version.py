from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path


VERSION_FILE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "version.json"
)

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def load_version() -> str:
    if not VERSION_FILE.exists():
        raise FileNotFoundError(
            f"Version file not found: {VERSION_FILE}"
        )

    with VERSION_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    version = data.get("version")

    if not isinstance(version, str):
        raise ValueError(
            f"Invalid version value: {version!r}"
        )

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            f"Invalid version format: {version!r}. "
            "Expected MAJOR.MINOR.PATCH, e.g. 0.1.0"
        )

    return version


def bump_patch(version: str) -> str:
    major, minor, patch = map(int, version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def save_version(version: str) -> None:
    VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": version
    }

    # Write first, then replace atomically.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=VERSION_FILE.parent,
        prefix=".version_",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(payload, temp_file, indent=2)
        temp_file.write("\n")

    try:
        temp_path.replace(VERSION_FILE)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def main() -> None:
    current_version = load_version()
    new_version = bump_patch(current_version)

    save_version(new_version)

    print(
        f"AETHER version bumped: "
        f"{current_version} -> {new_version}"
    )


if __name__ == "__main__":
    main()