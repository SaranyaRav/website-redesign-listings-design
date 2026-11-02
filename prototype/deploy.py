"""
cPanel deploy-with-rollback.

Pipeline (each step is a method on ``Deployer`` so a workflow engine can wrap
them as activities):

    probe → backup → upload → verify manifest → two-rename swap → health checks
                                                                   └─ rollback on failure

Safety properties this file is built around:

* **Credentials never touch disk.** The API token comes from a Key Vault stub
  (backed by environment variables here) at run time and is redacted from logs.
* **Atomic cut-over.** The new release is fully uploaded and checksum-verified
  in ``releases/<id>/`` before anything touches ``public_html``. The swap is
  two renames, so there is never a half-old/half-new ``public_html``.
* **Rollback is automatic** on any failed post-deploy check or any exception
  after the swap, and restores the exact previous directory.
* **Reruns are idempotent.** Deploying a release id that is already live is a
  no-op; nothing is uploaded and nothing is renamed.

The HTTP client is injected so tests can run against the in-process mock
(``fastapi.testclient.TestClient``) and production can pass a real ``httpx.Client``
pointed at ``https://<host>:2083``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree

import httpx

log = logging.getLogger("deploy")

RELEASE_MARKER = ".release.json"
HOME = ""  # UAPI paths are relative to the account home directory


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
class KeyVaultStub:
    """
    Stand-in for Azure Key Vault.

    Production: ``SecretClient(vault_url, DefaultAzureCredential()).get_secret(name)``.
    Here a secret named ``cpanel/site-1`` is read from the environment variable
    ``KV_CPANEL_SITE_1``. The value is never written anywhere by this program.
    """

    def get_secret(self, name: str) -> str:
        env_key = "KV_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
        value = os.environ.get(env_key)
        if not value:
            raise RuntimeError(f"secret {name!r} not found (expected env var {env_key})")
        return value


class RedactingFilter(logging.Filter):
    """Replace every occurrence of a secret in log output with ***."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for s in self._secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, ()
        return True


# --------------------------------------------------------------------------- #
# cPanel UAPI client (subset)
# --------------------------------------------------------------------------- #
class UAPIError(RuntimeError):
    pass


class CPanelClient:
    def __init__(self, http: httpx.Client, user: str, token: str) -> None:
        self._http = http
        self._headers = {"Authorization": f"cpanel {user}:{token}"}

    def _call(self, func: str, *, method: str = "GET", **kwargs) -> dict:
        url = f"/execute/Fileman/{func}"
        r = self._http.request(method, url, headers=self._headers, **kwargs)
        if r.status_code != 200:
            raise UAPIError(f"{func}: HTTP {r.status_code}")
        body = r.json()
        result = body.get("result", {})
        if result.get("status") != 1:
            raise UAPIError(f"{func}: {result.get('errors')}")
        return result.get("data")

    def list_files(self, path: str) -> list[dict]:
        return self._call("list_files", params={"dir": path})

    def exists(self, path: str) -> bool:
        parent, _, name = path.rstrip("/").rpartition("/")
        try:
            return any(e["file"] == name for e in self.list_files(parent or "/"))
        except UAPIError:
            return False

    def mkdir(self, path: str) -> None:
        parent, _, name = path.rstrip("/").rpartition("/")
        self._call("mkdir", params={"path": parent or "/", "name": name})

    def upload(self, remote_dir: str, filename: str, data: bytes) -> None:
        self._call(
            "upload_files",
            method="POST",
            data={"dir": remote_dir},
            files={"file-1": (filename, data)},
        )

    def get_file(self, path: str) -> bytes:
        d, _, f = path.rpartition("/")
        data = self._call("get_file_content", params={"dir": d or "/", "file": f})
        return data["content"].encode("utf-8")

    def rename(self, src: str, dst: str) -> None:
        self._call("fileop", params={"op": "rename", "sourcefiles": src, "destfiles": dst})

    def copy(self, src: str, dst: str) -> None:
        self._call("fileop", params={"op": "copy", "sourcefiles": src, "destfiles": dst})

    def unlink(self, path: str) -> None:
        self._call("fileop", params={"op": "unlink", "sourcefiles": path})

    def walk(self, path: str) -> list[str]:
        """Recursively list file paths (relative to home) under ``path``."""
        out: list[str] = []
        for entry in self.list_files(path):
            full = f"{path.rstrip('/')}/{entry['file']}"
            if entry["type"] == "dir":
                out.extend(self.walk(full))
            else:
                out.append(full)
        return out


# --------------------------------------------------------------------------- #
# Deployer
# --------------------------------------------------------------------------- #
@dataclass
class DeployResult:
    status: str                      # live | noop | rolled_back | rollback_failed
    release_id: str
    release_path: str | None = None
    backup_uri: str | None = None
    capabilities: dict = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    failure_reason: str | None = None
    steps: list[str] = field(default_factory=list)


class DeployError(RuntimeError):
    """Raised for failures *before* the swap; public_html has not been touched."""


class HealthCheckFailed(RuntimeError):
    """Raised for failures *after* the swap; rollback is required."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_manifest(build_dir: Path) -> dict[str, str]:
    """Relative path → sha256 for every file in the build."""
    return {
        p.relative_to(build_dir).as_posix(): sha256(p.read_bytes())
        for p in sorted(build_dir.rglob("*"))
        if p.is_file()
    }


def sitemap_paths(build_dir: Path) -> list[str]:
    sm = build_dir / "sitemap.xml"
    if not sm.exists():
        return []
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    tree = ElementTree.parse(sm)
    paths = []
    for loc in tree.getroot().findall("s:url/s:loc", ns):
        url = (loc.text or "").strip()
        path = re.sub(r"^https?://[^/]+", "", url) or "/"
        paths.append(path)
    return paths


class Deployer:
    def __init__(
        self,
        cp: CPanelClient,
        site_http: httpx.Client,
        site_base: str,
        blob_dir: Path,
        site_id: str,
        clock=time.time,
    ) -> None:
        self.cp = cp
        self.site_http = site_http
        self.site_base = site_base.rstrip("/")
        self.blob_dir = Path(blob_dir)
        self.site_id = site_id
        self._clock = clock

    # ----------------------------------------------------------------- steps
    def probe(self) -> dict:
        """Detect what the host supports. SSH is always 'no' for the mock."""
        caps = {"ssh": False, "uapi": False}
        try:
            self.cp.list_files("/")
            caps["uapi"] = True
        except UAPIError as e:
            raise DeployError(f"UAPI probe failed: {e}") from e
        log.info("probe: %s", caps)
        return caps

    def current_release(self) -> str | None:
        try:
            data = json.loads(self.cp.get_file(f"public_html/{RELEASE_MARKER}"))
            return data.get("release_id")
        except (UAPIError, ValueError):
            return None

    def backup(self, ts: str) -> str:
        """
        Copy public_html to releases/backup_<ts>/ on the host and stream a
        tar.gz of it to blob storage. Returns the blob URI.
        """
        host_backup = f"releases/backup_{ts}"
        self.cp.mkdir("releases")
        self.cp.copy("public_html", host_backup)

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for remote in self.cp.walk("public_html"):
                data = self.cp.get_file(remote)
                info = tarfile.TarInfo(name=remote)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        blob_path = self.blob_dir / "backups" / self.site_id / ts / "public_html.tar.gz"
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        blob_path.write_bytes(buf.getvalue())
        # Do not proceed unless the off-host copy is intact.
        if sha256(blob_path.read_bytes()) != sha256(buf.getvalue()):
            raise DeployError("blob backup checksum mismatch")
        uri = f"blob://backups/{self.site_id}/{ts}/public_html.tar.gz"
        log.info("backup: host=%s blob=%s", host_backup, uri)
        return uri

    def upload_release(self, build_dir: Path, release_id: str, manifest: dict[str, str]) -> str:
        release_path = f"releases/{release_id}"
        if self.cp.exists(release_path):
            self.cp.unlink(release_path)  # a previous partial attempt; start clean
        self.cp.mkdir("releases")
        self.cp.mkdir(release_path)
        made: set[str] = set()
        for rel in manifest:
            remote_dir = f"{release_path}/{Path(rel).parent.as_posix()}".rstrip("/.")
            if remote_dir not in made and remote_dir != release_path:
                self.cp.mkdir(remote_dir)
                made.add(remote_dir)
            self.cp.upload(remote_dir, Path(rel).name, (build_dir / rel).read_bytes())
        marker = json.dumps({"release_id": release_id, "manifest": manifest}, indent=1).encode()
        self.cp.upload(release_path, RELEASE_MARKER, marker)
        log.info("upload: %d files → %s", len(manifest), release_path)
        return release_path

    def verify_manifest(self, release_path: str, manifest: dict[str, str]) -> None:
        """Download every uploaded file and compare its sha256 to the local build."""
        mismatches = []
        for rel, expected in manifest.items():
            actual = sha256(self.cp.get_file(f"{release_path}/{rel}"))
            if actual != expected:
                mismatches.append(rel)
        if mismatches:
            raise DeployError(f"checksum mismatch after upload: {mismatches}")
        log.info("verify: manifest OK (%d files)", len(manifest))

    def swap(self, release_path: str, ts: str) -> str:
        """Two renames. The previous public_html survives as releases/prev_<ts>."""
        prev = f"releases/prev_{ts}"
        self.cp.rename("public_html", prev)
        try:
            self.cp.rename(release_path, "public_html")
        except UAPIError:
            # Second rename failed: put the old site straight back.
            self.cp.rename(prev, "public_html")
            raise
        log.info("swap: public_html → %s, %s → public_html", prev, release_path)
        return prev

    def health_checks(self, build_dir: Path) -> list[dict]:
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        r = self.site_http.get(f"{self.site_base}/")
        check("homepage_200", r.status_code == 200, f"HTTP {r.status_code}")
        check("security_headers", r.headers.get("x-content-type-options") == "nosniff")

        for path in sitemap_paths(build_dir):
            r = self.site_http.get(f"{self.site_base}{path}")
            check(f"sitemap:{path}", r.status_code == 200, f"HTTP {r.status_code}")

        if (build_dir / "contact.php").exists():
            r = self.site_http.post(
                f"{self.site_base}/contact.php",
                data={"name": "Deploy Bot", "message": "post-deploy test", "_test": "1"},
            )
            ok = r.status_code == 200 and r.json().get("ok") is True and r.json().get("test") is True
            check("contact_form_test_submission", ok, f"HTTP {r.status_code}")

        failed = [c["name"] for c in checks if not c["ok"]]
        if failed:
            raise HealthCheckFailed(f"failed checks: {failed}")
        log.info("health: %d checks passed", len(checks))
        return checks

    def rollback(self, prev: str, release_id: str, ts: str) -> None:
        """Reverse the swap. The failed release is kept for inspection."""
        failed_dir = f"releases/failed_{release_id}_{ts}"
        if self.cp.exists("public_html"):
            self.cp.rename("public_html", failed_dir)
        self.cp.rename(prev, "public_html")
        log.warning("rollback: restored %s → public_html; failed release kept at %s", prev, failed_dir)

    # ------------------------------------------------------------------ main
    def deploy(self, build_dir: Path, release_id: str) -> DeployResult:
        build_dir = Path(build_dir)
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime(self._clock()))
        result = DeployResult(status="pending", release_id=release_id)

        def step(name: str) -> None:
            result.steps.append(name)

        result.capabilities = self.probe()
        step("probe")

        if self.current_release() == release_id:
            log.info("release %s is already live; nothing to do", release_id)
            result.status = "noop"
            step("noop")
            return result

        manifest = build_manifest(build_dir)
        if not manifest:
            raise DeployError("build directory is empty")

        # ---- everything before the swap leaves public_html untouched ----
        try:
            result.backup_uri = self.backup(ts)
            step("backup")
            result.release_path = self.upload_release(build_dir, release_id, manifest)
            step("upload")
            self.verify_manifest(result.release_path, manifest)
            step("verify_manifest")
        except DeployError as e:
            log.error("aborting before swap: %s", e)
            if result.release_path and self.cp.exists(result.release_path):
                self.cp.unlink(result.release_path)
            result.status = "rolled_back"
            result.failure_reason = str(e)
            step("abort_before_swap")
            return result

        # ---- the swap and everything after it ----
        prev = self.swap(result.release_path, ts)
        step("swap")
        try:
            result.checks = self.health_checks(build_dir)
            step("health_checks")
        except Exception as e:  # noqa: BLE001 - any failure after the swap must roll back
            result.failure_reason = str(e)
            log.error("post-deploy failure: %s", e)
            try:
                self.rollback(prev, release_id, ts)
                result.status = "rolled_back"
                step("rollback")
            except UAPIError as re_err:
                result.status = "rollback_failed"
                result.failure_reason += f"; ROLLBACK FAILED: {re_err}; restore from {result.backup_uri}"
                log.critical("ROLLBACK FAILED for %s: %s — page on-call, backup at %s", self.site_id, re_err, result.backup_uri)
                step("rollback_failed")
            return result

        result.status = "live"
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Deploy a static build to cPanel with backup, verification and rollback.")
    p.add_argument("--build-dir", required=True, type=Path)
    p.add_argument("--release-id", default=None, help="defaults to sha256 of the build manifest")
    p.add_argument("--base-url", default="http://127.0.0.1:2083", help="cPanel UAPI base URL")
    p.add_argument("--site-url", default="http://127.0.0.1:2083/site", help="public URL of the site for health checks")
    p.add_argument("--site-id", default="site-1")
    p.add_argument("--user", default=os.environ.get("CPANEL_USER", "demo"))
    p.add_argument("--credential-ref", default=None, help="Key Vault secret name, e.g. cpanel/site-1")
    p.add_argument("--blob-dir", default=Path("./.blob"), type=Path, help="local stand-in for Blob Storage")
    args = p.parse_args(argv)

    if args.credential_ref:
        token = KeyVaultStub().get_secret(args.credential_ref)
    else:
        token = os.environ.get("CPANEL_API_TOKEN", "")
    if not token:
        print("No credential: set CPANEL_API_TOKEN or pass --credential-ref (backed by KV_* env vars).")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(RedactingFilter([token]))

    manifest = build_manifest(args.build_dir)
    release_id = args.release_id or sha256(json.dumps(manifest, sort_keys=True).encode())[:12]

    with httpx.Client(base_url=args.base_url, timeout=30) as cp_http, httpx.Client(timeout=30) as site_http:
        deployer = Deployer(
            CPanelClient(cp_http, args.user, token),
            site_http,
            args.site_url,
            args.blob_dir,
            args.site_id,
        )
        result = deployer.deploy(args.build_dir, release_id)

    print(json.dumps(result.__dict__, indent=2, default=str))
    return 0 if result.status in {"live", "noop"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
