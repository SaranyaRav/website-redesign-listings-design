# Prototype — cPanel deploy with backup, verification and rollback

This is the deploy stage of the website-redesign pipeline (design doc §1.2 Q3),
built as a self-contained prototype against a mock cPanel.

```
prototype/
├── deploy.py            probe → backup → upload → manifest verify → 2-rename swap → health checks → rollback
├── mock_cpanel/app.py   FastAPI app mimicking the UAPI Fileman calls, serving public_html over /site/
├── tests/               pytest: happy path, checksum mismatch, failed health check, rerun no-op, rollback failure, credentials
└── requirements.txt
```

## Run the tests

```bash
cd prototype
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m pytest -v
```

The tests run the mock in-process (`fastapi.testclient.TestClient`), so no
server or network is needed.

## Run it by hand

Terminal 1 — the mock cPanel on port 2083, with a scratch home directory. The
token is set in the environment and nowhere else:

```bash
export CPANEL_USER=demo CPANEL_API_TOKEN=local-dev-token MOCK_CPANEL_ROOT=./.mock_home
.venv/Scripts/python -m mock_cpanel.app
```

Terminal 2 — deploy a build. Credentials come from the Key Vault stub, which
reads `KV_CPANEL_SITE_1` for the secret named `cpanel/site-1`:

```bash
export KV_CPANEL_SITE_1=local-dev-token
.venv/Scripts/python deploy.py --build-dir ./example_build --credential-ref cpanel/site-1 \
    --base-url http://127.0.0.1:2083 --site-url http://127.0.0.1:2083/site
```

Run it twice: the second run reports `"status": "noop"` because the release id
(derived from the build manifest) is already live.

To see a rollback, remove a page that `sitemap.xml` still lists and deploy
again: the health check 404s, the two renames are reversed, and the failed
release is kept under `releases/failed_<id>_<ts>/` for inspection.

## What is mocked, and how it binds to real cPanel

| Prototype | Real system |
|---|---|
| `mock_cpanel` on `http://127.0.0.1:2083/execute/Fileman/*` | `https://<host>:2083/execute/Fileman/*` — same paths, same `Authorization: cpanel user:token` header, same response envelope. `CPanelClient` is unchanged; only `base_url` differs. |
| `KeyVaultStub` reading `KV_*` env vars | `azure.keyvault.secrets.SecretClient` with a managed identity. Same `get_secret(name)` interface. |
| `blob_dir` on local disk | `azure.storage.blob` container `backups/`, 30-day lifecycle rule. |
| `/site/...` served by the mock | The client's real domain over HTTPS. `site_http` is just an `httpx.Client` pointed elsewhere. |
| POST to `contact.php` answered by the mock | The deployed PHP form, which recognises the `_test=1` flag and routes to a test inbox. |
| `probe()` always reports `ssh: False` | Tries an SSH connection with our public key first; if it works, `upload_release`/`backup` use SFTP + remote `sha256sum` instead of UAPI. |
| Manifest verification downloads each file via `get_file_content` | Over SSH: one `sha256sum` across the release dir. Over UAPI only: the same download-and-hash loop, which is slower but the only honest check. |
| `Deployer` methods called sequentially in `deploy()` | Each method is a Temporal activity; `deploy()` is the workflow. Retries are per-activity; nothing after `swap()` is auto-retried. |

### Known simplifications

- `get_file_content` round-trips text, so the mock only supports UTF-8 files. Binary assets would go over SFTP in production.
- The mock has no quota, permission or PHP-version behaviour; the capability probe is therefore trivial here.
- TLS and redirect checks from the design are omitted because the mock serves plain HTTP with no `.htaccess` engine.

## Safety properties exercised by the tests

| Property | Test |
|---|---|
| Backup exists on host **and** off host before any write | `test_happy_path_goes_live_with_backup_and_atomic_swap` |
| Corrupted upload never reaches `public_html` | `test_checksum_mismatch_triggers_rollback_and_never_touches_public_html` |
| Any failed post-deploy check restores the previous site exactly | `test_failed_health_check_triggers_rollback`, `test_missing_sitemap_page_is_caught_by_health_check` |
| Re-deploying a live release does nothing | `test_rerun_with_same_release_is_noop` |
| Rollback failure is loud and names the backup | `test_rollback_failure_is_reported_not_swallowed` |
| Bad token is refused; good token never logged | `test_wrong_token_is_rejected`, `test_token_is_redacted_from_logs` |
