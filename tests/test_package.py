"""The model package handling (package.py), which needs no Home Assistant: python -m pytest tests"""

import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest

_spec = importlib.util.spec_from_file_location(
    "home3d_package", Path(__file__).parents[1] / "custom_components/home3d/package.py"
)
package = importlib.util.module_from_spec(_spec)
sys.modules["home3d_package"] = package
_spec.loader.exec_module(package)


def make_zip(path: Path, home: str = "flat", files: dict[str, bytes] | None = None) -> Path:
    files = files if files is not None else {
        "home.json": json.dumps({"id": home, "title": "Flat"}).encode(),
        "site/index.html": b"<!doctype html>",
        "site/assets/app.js": b"console.log(1)",
    }
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("home3d-model-flat-1.0.2.zip", ("flat", "1.0.2")),
        ("home3d-model-house2-2026.11.3.zip", ("house2", "2026.11.3")),
        ("home3d-model-my_home-1.0.0-beta.1.zip", ("my_home", "1.0.0-beta.1")),
        ("home3d-1.0.4.zip", None),  # not a model package
        ("home3d-model-flat.zip", None),
        ("home3d-model-Flat-1.0.zip", None),
        ("other-flat-1.0.zip", None),
        ("home3d-model-flat-1.0.tar.gz", None),
    ],
)
def test_parse_asset(name, expected):
    assert package.parse_asset(name) == expected


def install(store, version, **kw):
    return store.install(make_zip(store.new_download(0), **kw), version)


def test_install_makes_current_and_keeps_previous(tmp_path):
    store = package.ModelStore(tmp_path, "flat")
    assert store.current_version() is None and store.site() is None

    meta = install(store, "1.0.1")
    assert meta == {"id": "flat", "title": "Flat", "version": "1.0.1"}
    assert store.current_version() == "1.0.1"
    assert (store.site() / "index.html").is_file()
    assert store.home_json()["version"] == "1.0.1"

    install(store, "1.0.2")
    install(store, "1.0.3")
    assert store.current_version() == "1.0.3"
    left = sorted(p.name for p in store.root.iterdir())
    assert left == ["1.0.2", "1.0.3", "current"]  # the previous one stays; older ones, downloads, staging go


def test_reinstall_same_version(tmp_path):
    store = package.ModelStore(tmp_path, "flat")
    install(store, "1.0.1")
    install(store, "1.0.1")
    assert store.current_version() == "1.0.1"
    assert sorted(p.name for p in store.root.iterdir()) == ["1.0.1", "current"]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"site/index.html": b"x"}, "home.json"),
        ({"home.json": b"{", "site/index.html": b"x"}, "home.json"),
        ({"home.json": b'{"id": "other"}', "site/index.html": b"x"}, "not for the home"),
        ({"home.json": b'{"id": "flat"}'}, "site/index.html"),
        ({"home.json": b'{"id": "flat"}', "site/index.html": b"x", "../evil": b"x"}, "unsafe path"),
    ],
)
def test_bad_package_leaves_current_model(tmp_path, files, message):
    store = package.ModelStore(tmp_path, "flat")
    install(store, "1.0.1")
    with pytest.raises(package.PackageError, match=message):
        install(store, "1.0.2", files=files)
    assert store.current_version() == "1.0.1"
    assert sorted(p.name for p in store.root.iterdir()) == ["1.0.1", "current"]
    assert not (tmp_path / "evil").exists()


def test_not_a_zip_and_corrupt_member(tmp_path):
    store = package.ModelStore(tmp_path, "flat")
    bad = store.new_download(0)
    bad.write_bytes(b"not a zip")
    with pytest.raises(package.PackageError, match="broken package"):
        store.install(bad, "1.0.1")
    zip_path = make_zip(store.new_download(0))
    data = bytearray(zip_path.read_bytes())
    i = data.find(b"<!doctype html>")
    data[i : i + 5] = b"XXXXX"  # the CRC no longer matches
    zip_path.write_bytes(bytes(data))
    with pytest.raises(package.PackageError, match="broken package"):
        store.install(zip_path, "1.0.1")
    assert store.current_version() is None


def test_unreadable_current_falls_back(tmp_path):
    store = package.ModelStore(tmp_path, "flat")
    install(store, "1.0.1")
    (store.root / "current").write_bytes(b"\xff\xfe")
    assert store.current_version() == "1.0.1"
    (store.root / "current").unlink()
    assert store.current_version() == "1.0.1"


def test_sweep_removes_leftovers(tmp_path):
    store = package.ModelStore(tmp_path, "flat")
    install(store, "1.0.1")
    (store.root / ".download-x.zip").write_bytes(b"partial")
    (store.root / ".new-abc").mkdir()
    (store.root / "current.tmp").write_text("1.0.2")
    store.sweep()
    assert sorted(p.name for p in store.root.iterdir()) == ["1.0.1", "current"]
    assert store.current_version() == "1.0.1"


def test_rejects_bad_version_and_home(tmp_path):
    store = package.ModelStore(tmp_path, "flat")
    with pytest.raises(package.PackageError):
        store.install(make_zip(tmp_path / "x.zip"), "../1.0")
    for home in ("", "..", "a/b", "Flat"):
        with pytest.raises(ValueError):
            package.ModelStore(tmp_path, home)
