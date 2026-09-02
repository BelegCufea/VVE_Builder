import sys
from pathlib import Path

import pytest

import build_mod
from build_mod import Entry, from_base36, main, scan, write_mapping


@pytest.mark.parametrize(
    "input_str, expected_int",
    [
        # Scenario A: Standard Cases (Single & Multi-character)
        ("0", 0),
        ("1", 1),
        ("9", 9),
        ("A", 10),
        ("Z", 35),
        ("10", 36),
        ("36", 114),
        ("ZZ", 1295),
        ("100", 1296),
        ("XYZ", 44027),
        
        # Scenario B: Mixed and Lower Casing
        ("a", 10),
        ("z", 35),
        ("xyz", 44027),
        ("XyZ", 44027),
        ("aBcD", 481261),
        
        # Scenario C: Boundary & Edge Cases
        ("", 0),
        ("00000A", 10),
        ("ZZZZZZ", 2176782335),
    ]
)
def test_from_base36_valid(input_str, expected_int):
    """Test that valid base-36 strings are correctly converted to integers."""
    assert from_base36(input_str) == expected_int


@pytest.mark.parametrize(
    "invalid_str",
    [
        # Scenario D: Invalid Inputs
        " A",       # Leading space
        "A ",       # Trailing space
        "A B",      # Embedded space
        "1A$",      # Special character
        "1A-B",     # Dash
        "1A.B",     # Period
        "_12",      # Underscore
        "12ä",      # Accented/Unicode char
        "12ñ",      # Tilde/Unicode char
        "한",       # Non-ASCII character
    ]
)
def test_from_base36_invalid(invalid_str):
    """Test that invalid base-36 strings raise ValueError."""
    with pytest.raises(ValueError):
        from_base36(invalid_str)


# ============================== scan() ====================================
# scan() walks an output directory tree for TSXXXXXX.WAV files, decodes each
# base36 strref, and returns (valid entries, skipped invalid-body files).


def test_scan_collects_valid_entries(tmp_path: Path):
    """Valid TSXXXXXX.WAV files are collected with the decoded strref/resref."""
    npc1 = tmp_path / "Aataqah"
    npc2 = tmp_path / "Acolyte Byron"
    npc1.mkdir()
    npc2.mkdir()
    (npc1 / "TS0008UC.wav").write_bytes(b"RIFF")
    (npc1 / "TS0008UN.wav").write_bytes(b"RIFF")
    (npc2 / "TS000RQE.wav").write_bytes(b"RIFF")

    entries, skipped = scan(tmp_path)

    assert skipped == []

    by_resref = {e.resref: e for e in entries}
    assert set(by_resref) == {"TS0008UC", "TS0008UN", "TS000RQE"}
    assert by_resref["TS0008UC"].strref == 11460
    assert by_resref["TS0008UN"].strref == 11471
    assert by_resref["TS000RQE"].strref == 35942

    # npc_subdir is relative to the scanned root, with posix separators.
    assert {e.npc_subdir for e in entries} == {"Aataqah", "Acolyte Byron"}


def test_scan_skips_file_when_from_base36_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Drive scan()'s skipped-branch: a filename that passes the regex but whose
    decode fails. (With the current *real* from_base36 and FILENAME_RE this is
    effectively unreachable, since every [0-9A-Za-z] char is valid base36 - so
    we monkeypatch from_base36 to fail on one file.)

    Also verifies that non-matching files are silently ignored, not skipped.
    (Uses character-distinct bodies rather than case-only variants, because
    Windows is a case-insensitive filesystem.)
    """
    npc = tmp_path / "NPC"
    npc.mkdir()
    # Valid 6-char body, but we'll monkeypatch decode to raise.
    bad = npc / "TS0000AB.wav"
    bad.write_bytes(b"RIFF")
    # Invalid bodies, e.g. wrong length, don't match the regex -> ignored,
    # and never appear in 'skipped'.
    (npc / "TS12345.wav").write_bytes(b"RIFF")      # 5-char body
    (npc / "TS~~~~~~.wav").write_bytes(b"RIFF")     # non-alnum body
    valid = npc / "TS0000AC.wav"
    valid.write_bytes(b"RIFF")

    real = build_mod.from_base36

    def flaky(body: str) -> int:
        if body == "0000AB":  # only this body fails
            raise ValueError("boom")
        return real(body)

    monkeypatch.setattr(build_mod, "from_base36", flaky)

    entries, skipped = scan(tmp_path)

    assert skipped == [bad]
    assert [e.resref for e in entries] == ["TS0000AC"]


def test_scan_ignores_non_matching_files(tmp_path: Path):
    """Non-TS, non-WAV, and non-file entries are ignored."""
    npc = tmp_path / "NPC"
    npc.mkdir()
    (npc / "BD37502.wav").write_bytes(b"RIFF")   # no TS prefix
    (npc / "billing.txt").write_text("not audio")  # not a wav
    (npc / ".hidden").write_bytes(b"x")

    entries, skipped = scan(tmp_path)
    assert entries == []
    assert skipped == []


def test_scan_handles_nested_dirs_and_casing(tmp_path: Path):
    """Nested subdirs and lowercase filenames/extensions are handled."""
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "ts0zz0a6.wav").write_bytes(b"RIFF")  # lowercase name (6-char body)
    (tmp_path / "TS0ZZ0A6.WAV").write_bytes(b"RIFF")  # uppercase ext

    entries, skipped = scan(tmp_path)
    assert skipped == []
    assert len(entries) == 2
    # resref preserves the original filename casing.
    assert {e.resref for e in entries} == {"ts0zz0a6", "TS0ZZ0A6"}


def test_scan_empty_dir(tmp_path: Path):
    entries, skipped = scan(tmp_path)
    assert entries == []
    assert skipped == []


# ============================== write_mapping() ============================


def test_write_mapping_creates_file_with_sorted_rows(tmp_path: Path):
    """Space-delimited strref->resref rows, sorted by strref."""
    entries = [
        Entry(strref=50, resref="TS00018", source_path=Path("x"), npc_subdir="NPC"),
        Entry(strref=1, resref="TS00001", source_path=Path("y"), npc_subdir="NPC"),
        Entry(strref=7, resref="TS00007", source_path=Path("z"), npc_subdir="NPC"),
    ]
    dest = tmp_path / "sub" / "mapping.txt"

    write_mapping(entries, dest)

    lines = dest.read_text(encoding="ascii").splitlines()
    assert lines == ["1 TS00001", "7 TS00007", "50 TS00018"]


def test_write_mapping_creates_parent_dirs(tmp_path: Path):
    entries = [Entry(strref=1, resref="TS00001", source_path=Path("x"), npc_subdir="NPC")]
    dest = tmp_path / "a" / "b" / "c" / "mapping.txt"

    write_mapping(entries, dest)

    assert dest.is_file()
    assert dest.parent.is_dir()


def test_write_mapping_empty_list_writes_empty_file(tmp_path: Path):
    dest = tmp_path / "mapping.txt"

    write_mapping([], dest)

    assert dest.read_text(encoding="ascii") == ""


# ============================== main() ===================================
# main() resolves its input (output/), staging dir (mod/) and tp2/tra/WeiDU
# sources relative to the current working directory - hence chdir into a
# temp dir. It also parses sys.argv itself, so it must be sanitized to avoid
# picking up pytest's own command-line arguments.
# GAME_DIRECTORY is left pointing at the real configured game install (an
# absolute path, unaffected by chdir) since strref validation needs a real
# dialog.tlk to check against.
#
# Log messages are asserted via caplog rather than capsys: build_mod's
# console handler is built from `sys.stdout` once at module-import time
# (in setup_logging()), so it keeps writing to that original stream object
# even after capsys swaps sys.stdout out for the duration of a test.


@pytest.fixture(autouse=True)
def _sanitize_argv(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", ["build_mod.py"])


def test_main_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    """Happy path: files copied, mapping written, tp2 renamed into place."""
    out = tmp_path / "output" / "NPC"
    out.mkdir(parents=True)
    wav = out / "TS000RQE.wav"
    wav.write_bytes(b"RIFF")
    (tmp_path / "setup.tp2").write_text("// test tp2")
    (tmp_path / "setup.tra").write_text("// test tra")
    weidu_exe = tmp_path / "weidu" / "weidu.exe"
    weidu_exe.parent.mkdir(parents=True)
    weidu_exe.write_bytes(b"MZ")

    monkeypatch.chdir(tmp_path)
    caplog.set_level("INFO", logger="build_mod")
    result = main()

    assert result == 0
    dest = tmp_path / "mod" / "ievo"
    assert (dest / "WAV" / "TS000RQE.wav").is_file()
    assert (dest / "mapping.txt").is_file()
    assert (dest / "setup-ievo.tp2").is_file()

    mapping = dest.joinpath("mapping.txt").read_text(encoding="ascii")
    assert "35942 TS000RQE\n" in mapping

    assert "Staged 1 WAV file(s)" in caplog.text


def test_main_missing_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    """Returns 1 and prints an error when output/ is absent."""
    (tmp_path / "setup.tp2").write_text("// test tp2")
    monkeypatch.chdir(tmp_path)
    caplog.set_level("INFO", logger="build_mod")

    assert main() == 1
    assert "output dir not found" in caplog.text


def test_main_missing_tp2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    """Returns 1 and prints an error when setup.tp2 is absent."""
    out = tmp_path / "output" / "NPC"
    out.mkdir(parents=True)
    (out / "TS000RQE.wav").write_bytes(b"RIFF")
    monkeypatch.chdir(tmp_path)
    caplog.set_level("INFO", logger="build_mod")

    assert main() == 1
    assert "tp2 source not found" in caplog.text


def test_main_warns_on_invalid_and_stages_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    """
    A file that matches the regex but whose decode fails triggers the warning
    (reachable only by monkeypatching from_base36, see scan() tests above),
    while the genuinely-valid file still stages.
    """
    out = tmp_path / "output" / "NPC"
    out.mkdir(parents=True)
    valid = out / "TS0000AC.wav"
    valid.write_bytes(b"RIFF")  # valid
    bad = out / "TS0000AB.wav"  # passes regex, decode fails via patch
    bad.write_bytes(b"RIFF")
    (tmp_path / "setup.tp2").write_text("// test tp2")
    (tmp_path / "setup.tra").write_text("// test tra")
    weidu_exe = tmp_path / "weidu" / "weidu.exe"
    weidu_exe.parent.mkdir(parents=True)
    weidu_exe.write_bytes(b"MZ")

    real = build_mod.from_base36

    def flaky(body: str) -> int:
        if body == "0000AB":
            raise ValueError("boom")
        return real(body)

    monkeypatch.setattr(build_mod, "from_base36", flaky)
    monkeypatch.chdir(tmp_path)
    caplog.set_level("INFO", logger="build_mod")
    assert main() == 0

    assert "1 file(s) matched the TS prefix" in caplog.text
    assert "TS0000AB.wav" in caplog.text
    dest = tmp_path / "mod" / "ievo" / "WAV"
    assert (dest / "TS0000AC.wav").is_file()
    assert not (dest / "TS0000AB.wav").exists()
