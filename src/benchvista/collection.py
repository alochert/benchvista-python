"""A collection repository and its published site (`docs/library.md` §8).

A collection is a Git repository with `collection.json` at its root and one folder per item:
`spark-plugs/<id>/` and `analysis-steps/<id>/`, each with its `benchvista.json`. `build` checks
and tests every item, decides which versions are new, and writes the site GitHub Pages serves:

    index.html                 the browsable collection
    collection.json            every item, every version, every file hash
    checksums.txt              every published file in `sha256sum` format
    v/<version sha256>/<path>  the files of each version, by version hash

A released version is frozen. Its release record (`release.json`) is attached to a GitHub Release
named `<id>@<version>` when it is first published, and later builds use that record as it was:
publishing the same number with different files is refused (`LIBRARY_VERSION_REUSED`). Older
versions' files come from their release tags and are checked against their records.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from .library import KINDS, PACKAGE_FILE, Version, checksums_text, read_version

COLLECTION_FILE = "collection.json"
INDEX_FORMAT = "benchvista-collection-v1"
GROUPS = {"spark-plugs": "spark-plug", "analysis-steps": "analysis-step"}
REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
CONTROL = re.compile("[\\u0000-\\u001f\\u007f-\\u009f\\u2028\\u2029]")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class CollectionError(ValueError):
    def __init__(self, code: str, where: str) -> None:
        super().__init__(f"{code} at {where}")
        self.code = code
        self.where = where


def check_collection(value: Any) -> dict:
    """`collection.json`: the collection's name, summary, maintainer and GitHub repository."""
    fields = {"name": 64, "summary": 240, "maintainer": 64}
    if not isinstance(value, dict) or set(value) != set(fields) | {"repository"}:
        raise CollectionError("COLLECTION_FILE", COLLECTION_FILE)
    for key, maximum in fields.items():
        text = value[key]
        if not isinstance(text, str) or not 1 <= len(text) <= maximum or not text.strip() or CONTROL.search(text):
            raise CollectionError("COLLECTION_FILE", key)
    if not isinstance(value["repository"], str) or not REPOSITORY.match(value["repository"]):
        raise CollectionError("COLLECTION_FILE", "repository")
    return value


def item_folders(root: Path) -> list[Path]:
    """Every item folder, in group then name order. A folder is named after its item's id and
    sits in its kind's group; ids are unique across the collection."""
    folders: list[Path] = []
    ids: set[str] = set()
    for group, kind in GROUPS.items():
        base = root / group
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir(), key=lambda path: path.name):
            if folder.name.startswith(".") or not folder.is_dir():
                continue
            package_path = folder / PACKAGE_FILE
            if not package_path.is_file():
                raise CollectionError("LIBRARY_PACKAGE_MISSING", f"{group}/{folder.name}")
            package = json.loads(package_path.read_text("utf-8"))
            if package.get("kind") != kind or package.get("id") != folder.name:
                raise CollectionError("COLLECTION_ITEM_FOLDER", f"{group}/{folder.name}: holds {package.get('kind')} {package.get('id')}")
            if folder.name in ids:
                raise CollectionError("COLLECTION_ITEM_DUPLICATE", folder.name)
            ids.add(folder.name)
            folders.append(folder)
    if not folders:
        raise CollectionError("COLLECTION_EMPTY", f"add spark-plugs/<id>/ or analysis-steps/<id>/ with a {PACKAGE_FILE}")
    return folders


def load_releases(folder: Path | None) -> dict[str, dict]:
    """Published release records by tag, from `<folder>/<tag>/release.json`."""
    found: dict[str, dict] = {}
    if folder is None or not folder.is_dir():
        return found
    for path in sorted(folder.glob("*/release.json")):
        record = json.loads(path.read_text("utf-8"))
        tag = f"{record.get('id')}@{record.get('version')}"
        if path.parent.name != tag or record.get("kind") not in KINDS or not SHA256.match(str(record.get("sha256"))):
            raise CollectionError("COLLECTION_RELEASE_RECORD", path.parent.name)
        found[tag] = record
    return found


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


def released_at(root: Path, ref: str) -> str:
    """The commit time of `ref`, as an ISO date and time in UTC."""
    return _git(root, "log", "-1", "--format=%cI", ref).decode().strip()


def tagged_files(root: Path, tag: str, folder: str) -> dict[str, bytes]:
    """The files of an item folder at a release tag."""
    try:
        archive = _git(root, "archive", "--format=tar", f"refs/tags/{tag}", "--", folder)
    except subprocess.CalledProcessError as error:
        raise CollectionError("COLLECTION_RELEASE_FILES", f"{tag}: no tag with {folder}") from error
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                extracted = tar.extractfile(member)
                if extracted is not None:
                    files[member.name[len(folder) + 1 :]] = extracted.read()
    return files


def _version_of(files: dict[str, bytes]) -> Version:
    with tempfile.TemporaryDirectory() as directory:
        for path, data in files.items():
            target = Path(directory) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return read_version(Path(directory))


def _order(record: dict) -> tuple[int, ...]:
    return tuple(int(part) for part in record["version"].split("."))


def build(
    root: Path,
    out: Path,
    *,
    releases: Path | None = None,
    new_releases: Path | None = None,
    commit: str | None = None,
    attested: bool = False,
    run_tests: bool = True,
) -> dict:
    """Check and test every item and write the site to `out`. New versions' release records go
    to `new_releases/<tag>/`, for the workflow to publish as GitHub Releases."""
    from .cli import Refused, release_record, test_item  # the command's own checks

    root = root.resolve()
    collection = check_collection(json.loads((root / COLLECTION_FILE).read_text("utf-8")))
    published = load_releases(releases)
    records: dict[str, dict] = dict(published)
    contents: dict[str, dict[str, bytes]] = {}
    current_ids: list[str] = []
    for folder in item_folders(root):
        try:
            record = release_record(folder)
        except Refused as error:
            raise CollectionError(error.code, f"{folder.relative_to(root)}: {error.where}") from error
        if run_tests and test_item(folder) != 0:
            raise CollectionError("COLLECTION_TEST_FAILED", str(folder.relative_to(root)))
        tag = f"{record['id']}@{record['version']}"
        current_ids.append(record["id"])
        version = read_version(folder)
        files = {f["path"]: (folder / f["path"]).read_bytes() for f in version.files}
        if tag in published:
            if published[tag]["sha256"] != record["sha256"]:
                raise CollectionError("LIBRARY_VERSION_REUSED", f"{tag}: its files changed since it was published; raise the version number")
            contents[tag] = files
            continue
        record["tag"] = tag
        if commit:
            record["commit"] = commit
            record["released"] = released_at(root, commit)
        records[tag] = record
        contents[tag] = files
        if new_releases is not None:
            target = new_releases / tag
            target.mkdir(parents=True, exist_ok=True)
            (target / "release.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", "utf-8")
            (target / "checksums.txt").write_text(checksums_text(version), "utf-8")

    # Every published version stays listed and downloadable, from its tag, checked again here.
    for tag, record in published.items():
        if tag in contents:
            continue
        group = next(group for group, kind in GROUPS.items() if kind == record["kind"])
        files = tagged_files(root, tag, f"{group}/{record['id']}")
        if _version_of(files).sha256 != record["sha256"]:
            raise CollectionError("LIBRARY_CHECKSUM_MISMATCH", f"{tag}: the tag's files differ from its release record")
        contents[tag] = files

    out.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for tag, record in sorted(records.items()):
        for file in record["files"]:
            data = contents[tag][file["path"]]
            if hashlib.sha256(data).hexdigest() != file["sha256"]:
                raise CollectionError("LIBRARY_CHECKSUM_MISMATCH", f"{tag}/{file['path']}")
            target = out / "v" / record["sha256"] / file["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            lines.append(f"{file['sha256']}  v/{record['sha256']}/{file['path']}\n")

    by_id: dict[str, list[dict]] = {}
    for record in records.values():
        by_id.setdefault(record["id"], []).append(record)
    order = current_ids + sorted(set(by_id) - set(current_ids))
    reviewed_path = root / "reviewed.json"
    reviewed = json.loads(reviewed_path.read_text("utf-8")).get("reviewed", []) if reviewed_path.is_file() else []
    index = {
        "format": INDEX_FORMAT,
        **collection,
        "commit": commit,
        "attested": attested,
        "reviewed": reviewed,
        "items": [
            {"id": item_id, "kind": by_id[item_id][0]["kind"], "versions": sorted(by_id[item_id], key=_order, reverse=True)}
            for item_id in order
        ],
    }
    (out / "collection.json").write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", "utf-8")
    (out / "checksums.txt").write_text("".join(lines), "utf-8")
    (out / ".nojekyll").write_text("", "utf-8")
    (out / "index.html").write_text(render_site(index), "utf-8")
    return index


def _script_json(value: Any) -> str:
    """JSON safe inside a `<script>` element."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def site_text() -> dict:
    """The site's messages: English and each locale's current drafts, written by `npm run i18n:build`."""
    return json.loads(resources.files("benchvista").joinpath("site/text.json").read_text("utf-8"))


def render_site(index: dict) -> str:
    template = resources.files("benchvista").joinpath("site/template.html").read_text("utf-8")
    escape = lambda value: value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")  # noqa: E731
    values = {
        "__TITLE__": escape(index["name"]),
        "__SUMMARY__": escape(index["summary"]),
        "__DATA__": _script_json(index),
        "__TEXT__": _script_json(site_text()),
    }
    return re.sub("__(TITLE|SUMMARY|DATA|TEXT)__", lambda match: values[match.group(0)], template)
