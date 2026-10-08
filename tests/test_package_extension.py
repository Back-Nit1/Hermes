import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from package_extension import build  # noqa: E402


def test_package_is_reproducible_and_excludes_dev_files(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    zip_a, sha_a = build(tmp_path / "a")
    zip_b, sha_b = build(tmp_path / "b")
    assert sha_a == sha_b
    names = zipfile.ZipFile(zip_a).namelist()
    assert names == sorted(names) and "manifest.json" in names
    assert not any(n.startswith("tests/") or n == "package.json" for n in names)
