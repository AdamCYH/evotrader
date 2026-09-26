"""Regression tests: the broker token is readable only by its owner, and never printed.

Found 2026-09-26 while checking the sign-in for the public release. The token
files were written with default permissions (readable by every account on the
computer, in a folder anyone could list), though whoever holds the token can
place orders on the account. And every sign-in printed a debugging block to
the terminal — the first 20 characters of the access token and a call stack —
which looks like a crash to a newcomer and lands in `./run.sh offline`'s log.
"""

from __future__ import annotations

import logging
import stat
import sys
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from evotrader.mcp.oauth import FileTokenStorage

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")

ACCESS = "eyJhbGciOiJSUzI1NiJ9.secret-access-part-0123456789"
REFRESH = "refresh-secret-abcdefghijklmnop"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def token() -> OAuthToken:
    return OAuthToken(
        access_token=ACCESS, token_type="Bearer", expires_in=3600, refresh_token=REFRESH
    )


async def test_saved_files_are_for_the_owner_only(tmp_path: Path) -> None:
    storage = FileTokenStorage(tmp_path / "oauth" / "server")
    await storage.set_tokens(token())
    await storage.set_client_info(
        OAuthClientInformationFull(client_id="client-1", redirect_uris=["http://localhost/cb"])
    )

    assert mode(storage.cache_dir) == 0o700
    for saved in ("tokens.json", "expiry.json", "client.json"):
        assert mode(storage.cache_dir / saved) == 0o600, saved


async def test_files_saved_before_are_tightened(tmp_path: Path) -> None:
    folder = tmp_path / "oauth" / "server"
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    (folder / "tokens.json").write_text(token().model_dump_json())
    (folder / "tokens.json").chmod(0o644)

    storage = FileTokenStorage(folder)

    assert mode(folder) == 0o700
    assert mode(folder / "tokens.json") == 0o600
    assert (await storage.get_tokens()).access_token == ACCESS, "still readable by its owner"


async def test_no_part_of_the_token_is_printed_or_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    storage = FileTokenStorage(tmp_path / "oauth" / "server")
    await storage.set_tokens(token())
    await storage.get_tokens()

    printed = capsys.readouterr()
    shown = printed.out + printed.err + caplog.text
    for secret in (ACCESS, REFRESH):
        for start in range(len(secret) - 8):
            assert secret[start : start + 8] not in shown, "a piece of the token was shown"
    assert printed.out == "", "nothing printed to the terminal"
