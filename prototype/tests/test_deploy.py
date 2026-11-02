from __future__ import annotations

import logging
import tarfile
from pathlib import Path

import pytest

from deploy import RedactingFilter, UAPIError
from tests.conftest import TOKEN, USER, write_build


def public_html(home: Path) -> str:
    return (home / "public_html" / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_happy_path_goes_live_with_backup_and_atomic_swap(deployer, home, build, tmp_path):
    result = deployer.deploy(build, "v1")

    assert result.status == "live", result
    assert result.steps == ["probe", "backup", "upload", "verify_manifest", "swap", "health_checks"]
    assert public_html(home) == "<h1>Home v1</h1>"
    assert (home / "public_html" / "assets" / "style.css").exists()
    assert (home / "public_html" / ".release.json").exists()

    # previous site is preserved on the host under releases/prev_<ts>
    prev_dirs = list((home / "releases").glob("prev_*"))
    assert len(prev_dirs) == 1
    assert (prev_dirs[0] / "index.html").read_text(encoding="utf-8") == "<h1>OLD SITE</h1>"
    assert (prev_dirs[0] / ".htaccess").exists()

    # and a tar.gz of it went to "blob storage"
    blobs = list((tmp_path / "blob").rglob("public_html.tar.gz"))
    assert len(blobs) == 1
    with tarfile.open(blobs[0]) as tar:
        assert "public_html/index.html" in tar.getnames()
    assert result.backup_uri.startswith("blob://backups/site-1/")

    # all health checks recorded and passing
    names = {c["name"] for c in result.checks}
    assert {"homepage_200", "security_headers", "sitemap:/about.html", "contact_form_test_submission"} <= names
    assert all(c["ok"] for c in result.checks)


# --------------------------------------------------------------------------- #
# Checksum mismatch → abort before swap, public_html untouched
# --------------------------------------------------------------------------- #
def test_checksum_mismatch_triggers_rollback_and_never_touches_public_html(deployer, app, home, build):
    app.state.faults["corrupt_upload"] = True

    result = deployer.deploy(build, "v1")

    assert result.status == "rolled_back"
    assert "checksum mismatch" in result.failure_reason
    assert "swap" not in result.steps
    assert public_html(home) == "<h1>OLD SITE</h1>"
    # the corrupted release directory was cleaned up
    assert not (home / "releases" / "v1").exists()
    # backup still happened before the upload
    assert result.backup_uri is not None


# --------------------------------------------------------------------------- #
# Failed health check → rollback after swap
# --------------------------------------------------------------------------- #
def test_failed_health_check_triggers_rollback(deployer, app, home, build):
    app.state.faults["serve_500_paths"].add("about.html")

    result = deployer.deploy(build, "v1")

    assert result.status == "rolled_back"
    assert "sitemap:/about.html" in result.failure_reason
    assert result.steps[-2:] == ["swap", "rollback"]
    # old site is back
    assert public_html(home) == "<h1>OLD SITE</h1>"
    assert (home / "public_html" / ".htaccess").exists()
    # failed release kept for inspection
    failed = list((home / "releases").glob("failed_v1_*"))
    assert len(failed) == 1
    assert (failed[0] / "index.html").read_text(encoding="utf-8") == "<h1>Home v1</h1>"


def test_missing_sitemap_page_is_caught_by_health_check(deployer, home, build):
    # Sitemap advertises a page the build does not contain → 404 → rollback.
    (build / "about.html").unlink()

    result = deployer.deploy(build, "v1")

    assert result.status == "rolled_back"
    assert public_html(home) == "<h1>OLD SITE</h1>"


# --------------------------------------------------------------------------- #
# Rerun with the same release is a no-op
# --------------------------------------------------------------------------- #
def test_rerun_with_same_release_is_noop(deployer, home, build):
    first = deployer.deploy(build, "v1")
    assert first.status == "live"
    releases_before = sorted(p.name for p in (home / "releases").iterdir())

    second = deployer.deploy(build, "v1")

    assert second.status == "noop"
    assert second.steps == ["probe", "noop"]
    assert sorted(p.name for p in (home / "releases").iterdir()) == releases_before
    assert public_html(home) == "<h1>Home v1</h1>"


def test_new_release_after_live_one_swaps_again(deployer, home, build, tmp_path):
    deployer.deploy(build, "v1")
    build2 = write_build(tmp_path, "v2", pages=("about", "services"))

    result = deployer.deploy(build2, "v2")

    assert result.status == "live"
    assert public_html(home) == "<h1>Home v2</h1>"
    assert (home / "public_html" / "services.html").exists()


# --------------------------------------------------------------------------- #
# Rollback itself fails → loud failure with backup pointer
# --------------------------------------------------------------------------- #
def test_rollback_failure_is_reported_not_swallowed(deployer, app, home, build):
    app.state.faults["serve_500_paths"].add("about.html")

    # The swap renames public_html too, so make the fault bite only on the rollback's rename:
    original_rename = deployer.cp.rename
    calls = {"n": 0}

    def flaky_rename(src, dst):
        calls["n"] += 1
        if src == "public_html" and calls["n"] > 1:
            raise UAPIError("fileop: Permission denied")
        return original_rename(src, dst)

    deployer.cp.rename = flaky_rename

    result = deployer.deploy(build, "v1")

    assert result.status == "rollback_failed"
    assert "ROLLBACK FAILED" in result.failure_reason
    assert result.backup_uri in result.failure_reason


# --------------------------------------------------------------------------- #
# Credentials: wrong token is refused, token never appears in logs
# --------------------------------------------------------------------------- #
def test_wrong_token_is_rejected(client, build, tmp_path):
    from deploy import CPanelClient, Deployer, DeployError

    bad = Deployer(CPanelClient(client, USER, "wrong"), client, "http://testserver/site", tmp_path / "b", "s")
    with pytest.raises(DeployError, match="UAPI probe failed"):
        bad.deploy(build, "v1")


def test_token_is_redacted_from_logs(caplog):
    logger = logging.getLogger("deploy.redaction-test")
    logger.addFilter(RedactingFilter([TOKEN]))
    with caplog.at_level(logging.INFO):
        logger.info("Authorization: cpanel %s:%s", USER, TOKEN)
    assert TOKEN not in caplog.text
    assert "***" in caplog.text
