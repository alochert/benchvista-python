"""A collection repository: its site, releases, frozen versions and refusals."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from benchvista.collection import CollectionError, build, check_collection

EXAMPLES = Path(__file__).parents[1] / "examples"
COLLECTION = {
    "name": "Test collection",
    "summary": "Spark Plugs and steps for tests.",
    "maintainer": "Tests",
    "repository": "example/collection",
}


def git(root: Path, *args: str) -> str:
    command = ["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "spark-plugs").mkdir(parents=True)
    (root / "analysis-steps").mkdir()
    for item in ("console-messages", "gpio-loopback"):
        shutil.copytree(EXAMPLES / item, root / "spark-plugs" / item)
    shutil.copytree(EXAMPLES / "glitch-finder", root / "analysis-steps" / "glitch-finder", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "collection.json").write_text(json.dumps(COLLECTION))
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    return root


def publish(root: Path, tmp_path: Path, name: str, releases: Path | None = None) -> tuple[dict, Path, Path]:
    """Build like the workflow does: new records go to GitHub Releases, here a folder, and tags."""
    out, new = tmp_path / f"{name}-site", tmp_path / f"{name}-new"
    index = build(root, out, releases=releases, new_releases=new, commit=git(root, "rev-parse", "HEAD"), run_tests=False)
    store = tmp_path / "releases"
    for folder in sorted(new.iterdir()) if new.exists() else []:
        git(root, "tag", folder.name)
        (store / folder.name).mkdir(parents=True, exist_ok=True)
        shutil.copy(folder / "release.json", store / folder.name / "release.json")
    return index, out, store


def test_first_build_releases_every_item_and_publishes_its_files(repo, tmp_path):
    index, out, store = publish(repo, tmp_path, "first")
    assert [item["id"] for item in index["items"]] == ["console-messages", "gpio-loopback", "glitch-finder"]
    assert sorted(path.name for path in store.iterdir()) == ["console-messages@1.0.0", "glitch-finder@1.0.0", "gpio-loopback@1.0.0"]
    record = index["items"][1]["versions"][0]
    assert record["tag"] == "gpio-loopback@1.0.0" and record["commit"] == git(repo, "rev-parse", "HEAD")
    step = index["items"][2]["versions"][0]
    assert step["step"]["manifest"]["format"] == "benchvista-python-step-v1"
    lines = (out / "checksums.txt").read_text().splitlines()
    assert len(lines) == sum(len(item["versions"][0]["files"]) for item in index["items"])
    for line in lines:
        digest, path = line.split("  ")
        assert subprocess.run(["shasum", "-a", "256", str(out / path)], capture_output=True, text=True).stdout.split()[0] == digest
    html = (out / "index.html").read_text()
    assert "Test collection" in html and "collection.limits" in html and "</script>" in html
    assert "<" not in html.split('id="data">')[1].split("</script>")[0]


def test_a_new_version_keeps_the_old_one_listed_from_its_tag(repo, tmp_path):
    _, _, store = publish(repo, tmp_path, "first")
    package = repo / "spark-plugs" / "gpio-loopback" / "benchvista.json"
    item = json.loads(package.read_text())
    item["version"] = "1.1.0"
    package.write_text(json.dumps(item, indent=2))
    (repo / "spark-plugs" / "gpio-loopback" / "README.md").write_text("Changed.\n")
    git(repo, "commit", "-qam", "1.1.0")
    index, out, store = publish(repo, tmp_path, "second", store)
    versions = next(i for i in index["items"] if i["id"] == "gpio-loopback")["versions"]
    assert [v["version"] for v in versions] == ["1.1.0", "1.0.0"]
    old = versions[1]
    # The older version's files come from its tag, byte for byte.
    assert (out / "v" / old["sha256"] / "README.md").read_bytes() == subprocess.run(
        ["git", "-C", str(repo), "show", "gpio-loopback@1.0.0:spark-plugs/gpio-loopback/README.md"], capture_output=True, check=True
    ).stdout
    # Unchanged items are not released again.
    assert sorted(path.name for path in (tmp_path / "second-new").iterdir()) == ["gpio-loopback@1.1.0"]


def test_the_same_number_with_different_files_is_refused(repo, tmp_path):
    _, _, store = publish(repo, tmp_path, "first")
    (repo / "spark-plugs" / "gpio-loopback" / "README.md").write_text("Changed without a new number.\n")
    git(repo, "commit", "-qam", "edit")
    with pytest.raises(CollectionError) as raised:
        publish(repo, tmp_path, "second", store)
    assert raised.value.code == "LIBRARY_VERSION_REUSED"


def test_a_released_record_is_used_as_it_was_published(repo, tmp_path):
    _, _, store = publish(repo, tmp_path, "first")
    record_path = store / "glitch-finder@1.0.0" / "release.json"
    record = json.loads(record_path.read_text())
    record["step"]["manifest"]["sdk"]["version"] = "0.0.9"  # an older SDK built it
    record_path.write_text(json.dumps(record))
    index, _, _ = publish(repo, tmp_path, "second", store)
    step = next(i for i in index["items"] if i["id"] == "glitch-finder")["versions"][0]
    assert step["step"]["manifest"]["sdk"]["version"] == "0.0.9"


def test_a_tag_whose_files_differ_from_its_record_is_refused(repo, tmp_path):
    _, _, store = publish(repo, tmp_path, "first")
    package = repo / "spark-plugs" / "gpio-loopback" / "benchvista.json"
    item = json.loads(package.read_text())
    item["version"] = "1.1.0"
    package.write_text(json.dumps(item, indent=2))
    git(repo, "commit", "-qam", "1.1.0")
    git(repo, "tag", "-f", "gpio-loopback@1.0.0")  # the old tag moved to different files
    with pytest.raises(CollectionError) as raised:
        publish(repo, tmp_path, "second", store)
    assert raised.value.code == "LIBRARY_CHECKSUM_MISMATCH"


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (lambda root: shutil.move(root / "spark-plugs" / "gpio-loopback", root / "spark-plugs" / "loopback"), "COLLECTION_ITEM_FOLDER"),
        (lambda root: shutil.move(root / "analysis-steps" / "glitch-finder", root / "spark-plugs" / "glitch-finder"), "COLLECTION_ITEM_FOLDER"),
        (lambda root: (root / "spark-plugs" / "gpio-loopback" / "helpers.py").write_text("X = 1\n"), "LIBRARY_PLUG_EXTRA_SOURCE"),
    ],
)
def test_misplaced_or_invalid_items_are_refused(repo, tmp_path, edit, code):
    edit(repo)
    with pytest.raises(CollectionError) as raised:
        build(repo, tmp_path / "site", run_tests=False)
    assert raised.value.code == code


def test_a_failing_test_stops_the_build(repo, tmp_path):
    case = repo / "spark-plugs" / "gpio-loopback" / "tests" / "wired.json"
    case.write_text(json.dumps({"wiring": {"IN": "OUT"}, "expect": {"contains": ["FAIL"]}}))
    with pytest.raises(CollectionError) as raised:
        build(repo, tmp_path / "site")
    assert raised.value.code == "COLLECTION_TEST_FAILED"


def test_collection_file_is_checked():
    assert check_collection(dict(COLLECTION)) == COLLECTION
    for broken in ({**COLLECTION, "repository": "no-owner"}, {**COLLECTION, "name": ""}, {**COLLECTION, "extra": 1}):
        with pytest.raises(CollectionError):
            check_collection(broken)
