import os
import shutil
import subprocess

import pytest

from kessel_gateway import cli
from kessel_gateway.agent_skills import (
    SKILLS,
    bundled_skills_directory,
    install_skills,
)


def test_list_skills_needs_no_setup_or_provider_checks(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config"
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(config))

    def unexpected_check():
        pytest.fail("Listing skills must not probe providers")

    monkeypatch.setattr(cli, "check_providers", unexpected_check)

    assert cli.main(["skills"]) == 0
    output = capsys.readouterr().out
    assert all(name in output for name in SKILLS)
    assert not config.exists()


@pytest.mark.parametrize("agent_directory", [".agents/skills", ".claude/skills"])
def test_install_copies_standalone_skills_without_changing_config(
    tmp_path, monkeypatch, agent_directory
):
    config = tmp_path / "config"
    monkeypatch.setenv("KESSEL_CONFIG_DIR", str(config))
    destination = tmp_path / agent_directory
    unrelated = destination / "personal-skill" / "SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("personal instructions", encoding="utf-8")

    assert cli.main(["skills", "install", "--directory", str(destination)]) == 0

    for name in SKILLS:
        source = bundled_skills_directory() / name
        assert (destination / name / "SKILL.md").is_file()
        for resource in source.rglob("*"):
            if resource.is_file():
                installed = destination / name / resource.relative_to(source)
                assert installed.read_bytes() == resource.read_bytes()
    assert unrelated.read_text(encoding="utf-8") == "personal instructions"
    assert not config.exists()
    assert install_skills(destination) == [(name, False) for name in SKILLS]


@pytest.mark.parametrize("conflict", ["modified", "extra", "file"])
def test_conflict_preserves_existing_skills_and_does_not_copy_others(
    tmp_path, capsys, conflict
):
    destination = tmp_path / "skills"
    install_skills(destination)
    preserved = destination / "kessel-doctor" / "SKILL.md"
    if conflict == "modified":
        preserved.write_text("user customization", encoding="utf-8")
    elif conflict == "extra":
        (preserved.parent / "notes.md").write_text("user notes", encoding="utf-8")
    else:
        # A same-named ordinary file must not be replaced by a skill directory.
        preserved = destination / "kessel-doctor"
        shutil.rmtree(preserved)
        preserved.write_text("user file", encoding="utf-8")
    original = preserved.read_bytes()
    shutil.rmtree(destination / "kessel-setup")

    assert cli.main(["skills", "install", "--directory", str(destination)]) == 1

    assert preserved.read_bytes() == original
    assert not (destination / "kessel-setup").exists()
    assert "Existing skill differs" in capsys.readouterr().err


def test_install_requires_explicit_destination():
    with pytest.raises(SystemExit) as result:
        cli.main(["skills", "install"])
    assert result.value.code == 2


def test_install_rejects_file_destination(tmp_path, capsys):
    destination = tmp_path / "skills"
    destination.write_text("keep", encoding="utf-8")

    assert cli.main(["skills", "install", "--directory", str(destination)]) == 1
    assert destination.read_text(encoding="utf-8") == "keep"
    assert "not a directory" in capsys.readouterr().err


def test_install_rejects_destination_inside_bundle():
    with pytest.raises(ValueError, match="outside Kessel's bundled skills"):
        install_skills(bundled_skills_directory() / "kessel-setup" / "nested")


@pytest.mark.parametrize("location", ["parent", "skill", "resource"])
def test_install_refuses_links_without_writing_through_them(tmp_path, location):
    destination = tmp_path / "skills"
    external = tmp_path / "external"
    external.mkdir()
    if location == "parent":
        link = tmp_path / "linked-parent"
        destination = link / "skills"
    elif location == "skill":
        destination.mkdir()
        link = destination / "kessel-setup"
    else:
        install_skills(destination)
        link = destination / "kessel-setup" / "external"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this user")

    with pytest.raises(ValueError, match="link"):
        install_skills(destination)
    assert list(external.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions only")
@pytest.mark.parametrize("location", ["parent", "skill", "resource"])
def test_install_refuses_windows_junctions(tmp_path, location):
    destination = tmp_path / "skills"
    external = tmp_path / "external"
    external.mkdir()
    if location == "parent":
        link = tmp_path / "linked-parent"
        destination = link / "skills"
    elif location == "skill":
        destination.mkdir()
        link = destination / "kessel-setup"
    else:
        install_skills(destination)
        link = destination / "kessel-setup" / "external"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(external)],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    with pytest.raises(ValueError, match="link"):
        install_skills(destination)
    assert list(external.iterdir()) == []


def test_permission_error_returns_actionable_cli_failure(tmp_path, monkeypatch, capsys):
    def denied(*args):
        raise PermissionError("destination is read-only")

    monkeypatch.setattr("kessel_gateway.agent_skills.install_skills", denied)

    assert cli.main(["skills", "install", "--directory", str(tmp_path)]) == 1
    assert "destination is read-only" in capsys.readouterr().err
