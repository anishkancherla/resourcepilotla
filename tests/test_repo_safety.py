from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def _repo_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not any(part in SKIP_PARTS for part in path.parts)
    ]


def test_generated_secret_files_are_gitignored() -> None:
    for path in (".env", ".profiles.json", ".agent-data/example", "postgres-data/file", "x.log"):
        result = subprocess.run(
            ["git", "check-ignore", path], cwd=ROOT, check=False, capture_output=True, text=True
        )
        assert result.returncode == 0, path


def test_docker_context_excludes_secrets() -> None:
    ignored = (ROOT / ".dockerignore").read_text().split()
    for path in (".env", ".profiles.json", ".agent-data", "postgres-data", ".git"):
        assert path in ignored


def test_env_is_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode != 0


def test_no_common_live_token_patterns_in_repository() -> None:
    patterns = [
        re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
        re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{20,}"),
        re.compile(r"sk-(?:live|test|proj|ant)-[A-Za-z0-9_-]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ]
    for path in _repo_files():
        if path.name in {".env", ".profiles.json", "uv.lock"}:
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for pattern in patterns:
            assert not pattern.search(text), f"possible secret in {path.relative_to(ROOT)}"


def test_no_mainnet_or_real_money_switch() -> None:
    for path in _repo_files():
        shipped = path.suffix in {".py", ".yml", ".toml"} or path.name == ".env.example"
        if not shipped or "tests" in path.relative_to(ROOT).parts:
            continue
        text = path.read_text().lower()
        assert "fetchai_mainnet" not in text, path
        assert "network=mainnet" not in text and "fet_network=mainnet" not in text, path


def test_local_markdown_links_resolve() -> None:
    for document in ROOT.rglob("*.md"):
        if any(part in SKIP_PARTS for part in document.parts):
            continue
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", document.read_text()):
            clean = target.split("#", 1)[0]
            if not clean or "://" in clean or clean.startswith("mailto:"):
                continue
            assert (document.parent / clean).exists(), f"{document}: broken link {target}"


def test_documented_make_commands_exist() -> None:
    targets = set(
        re.findall(r"^([a-z][a-z-]*):", (ROOT / "Makefile").read_text(), flags=re.MULTILINE)
    )
    for document in [ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        if document.exists():
            commands = set(re.findall(r"\bmake ([a-z][a-z-]*)", document.read_text()))
            assert commands <= targets, f"{document.name}: {commands - targets}"
