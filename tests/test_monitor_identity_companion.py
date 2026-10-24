"""Authenticate across normal SQLite companion-file removal at the filesystem seam."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cloudyfan_jev.identity import IdentityError, IdentityStore


def test_authentication_allows_a_journal_that_disappeared_after_existence_check(tmp_path, monkeypatch):
    identity = IdentityStore(tmp_path / "identities.sqlite", pepper=b"synthetic-companion-pepper-only-0000", clock=lambda: 1000)
    identity.create_principal("native-client")
    credential = identity.issue("native-client", scopes={"tasks:read"}, expires_at=2000)
    companion = identity.path.with_name(identity.path.name + "-journal")
    real_stat = Path.stat
    def stale_existence(path, *args, **kwargs):
        # The journal existed at the existence probe and was removed by another
        # successful transaction before lstat. No real service files are touched.
        if path == companion and kwargs.get("follow_symlinks", True):
            return real_stat(identity.path)
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stale_existence)
    assert identity.authenticate(credential.token, "tasks:read") == "native-client"


@pytest.mark.parametrize("kind", ["symlink", "public_permissions"])
def test_authentication_still_rejects_unsafe_database_companions(tmp_path, kind):
    identity = IdentityStore(tmp_path / "identities.sqlite", pepper=b"synthetic-companion-pepper-only-0000", clock=lambda: 1000)
    identity.create_principal("native-client")
    credential = identity.issue("native-client", scopes={"tasks:read"}, expires_at=2000)
    companion = identity.path.with_name(identity.path.name + "-journal")
    if kind == "symlink":
        companion.symlink_to(tmp_path / "absent-target")
    else:
        companion.write_bytes(b"")
        companion.chmod(0o644)
    with pytest.raises(IdentityError, match="^Unsafe identity database companion$"):
        identity.authenticate(credential.token, "tasks:read")


def test_parallel_authentication_and_activity_checks_share_short_registry_transactions(tmp_path):
    identity = IdentityStore(tmp_path / "identities.sqlite", pepper=b"synthetic-parallel-pepper-only-0000", clock=lambda: 1000)
    identity.create_principal("native-client")
    credential = identity.issue("native-client", scopes={"tasks:read"}, expires_at=2000)
    def check(_):
        for _ in range(20):
            assert identity.is_active("native-client") is True
            assert identity.authenticate(credential.token, "tasks:read") == "native-client"
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(check, range(6)))
