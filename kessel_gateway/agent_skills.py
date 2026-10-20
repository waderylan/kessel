"""List and install the agent skills bundled with this Kessel release."""

from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path


SKILLS = {
    "kessel-setup": "Install Kessel, guide provider login, and verify local setup.",
    "kessel-connect": "Connect an application or editor, or create a working example.",
    "kessel-doctor": "Diagnose failures and repair or upgrade an existing installation.",
}


def bundled_skills_directory() -> Path:
    return Path(__file__).parent / "skills"


def _is_link(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    # Reparse points include Windows junctions, also on Python < 3.12 where
    # Path.is_junction is unavailable.
    return path.is_symlink() or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _files(directory: Path) -> dict[Path, bytes]:
    """Compare complete trees without following links in an existing install."""
    files = {}
    for path in directory.rglob("*"):
        if _is_link(path):
            raise ValueError(f"Skill directory contains a link: {path}")
        if path.is_file():
            files[path.relative_to(directory)] = path.read_bytes()
    return files


def install_skills(directory: Path) -> list[tuple[str, bool]]:
    """Copy all skills, preserving identical installs and refusing conflicts.

    Check every destination before copying any skill so a customized skill
    never causes an otherwise predictable partial install.
    """
    destination = directory.expanduser().absolute()
    for path in (destination, *destination.parents):
        if _is_link(path):
            raise ValueError(f"Choose a skill directory without links: {path}")
    if destination.resolve().is_relative_to(bundled_skills_directory().resolve()):
        raise ValueError("Choose a destination outside Kessel's bundled skills.")
    if destination.exists() and not destination.is_dir():
        raise ValueError(f"Skill destination is not a directory: {destination}")

    pending = []
    for name in SKILLS:
        source = bundled_skills_directory() / name
        target = destination / name
        if _is_link(target):
            raise ValueError(f"Refusing to replace linked skill: {target}")
        if target.exists():
            if not target.is_dir() or _files(target) != _files(source):
                raise ValueError(
                    f"Existing skill differs: {target}. Back it up and move it "
                    "aside, or choose another directory; no skills were copied."
                )
            pending.append((name, False))
        else:
            pending.append((name, True))

    for name, needed in pending:
        if needed:
            shutil.copytree(bundled_skills_directory() / name, destination / name)
    return pending


def command_skills(directory: Path | None = None) -> int:
    if directory is None:
        for name, description in SKILLS.items():
            print(f"{name}: {description}")
        print(f"\nBundled files: {bundled_skills_directory()}")
        print("Install for Codex: kessel skills install --directory .agents/skills")
        print("Install for Claude Code: kessel skills install --directory .claude/skills")
        print("Run in your project, or supply an absolute user skill directory.")
        print(
            "Installing skills copies instructions; it does not run setup "
            "or change agents' permissions."
        )
        return 0
    try:
        results = install_skills(directory)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name, installed in results:
        action = "Installed" if installed else "Already installed"
        print(f"{action}: {directory.expanduser().absolute() / name}")
    print("Start a new agent conversation if the skills are not listed yet.")
    print(
        "Choose kessel-setup, kessel-connect, or kessel-doctor; "
        "installation runs none of them."
    )
    return 0
