from pathlib import Path

from requiem.toolbelt import (
    FakeFileClient,
    FileMissing,
    FileRead,
    RealFileClient,
    Toolbelt,
)


def test_real_file_client_reads_existing(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("hello", encoding="utf-8")
    out = RealFileClient().read_text(p)
    assert isinstance(out, FileRead)
    assert out.content == "hello"


def test_real_file_client_reports_missing(tmp_path: Path):
    out = RealFileClient().read_text(tmp_path / "nope.txt")
    assert isinstance(out, FileMissing)


def test_fake_file_client_reads_canned():
    fake = FakeFileClient(by_path={Path("/a/b.txt"): "scripted"})
    out = fake.read_text(Path("/a/b.txt"))
    assert isinstance(out, FileRead)
    assert out.content == "scripted"


def test_toolbelt_real_factory():
    tb = Toolbelt.real()
    assert tb.files is not None
    assert tb.git is not None
