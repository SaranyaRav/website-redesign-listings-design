from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deploy import CPanelClient, Deployer  # noqa: E402
from mock_cpanel import create_app  # noqa: E402

USER = "demo"
TOKEN = "test-token-not-a-real-secret"  # test value for the mock only


def write_build(root: Path, release_label: str, pages: tuple[str, ...] = ("about",)) -> Path:
    build = root / f"build_{release_label}"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text(f"<h1>Home {release_label}</h1>", encoding="utf-8")
    for page in pages:
        (build / f"{page}.html").write_text(f"<h1>{page} {release_label}</h1>", encoding="utf-8")
    (build / "assets" / "style.css").write_text("body{margin:0}", encoding="utf-8")
    (build / "contact.php").write_text("<?php echo json_encode(['ok' => true]); ?>", encoding="utf-8")
    locs = "".join(f"<url><loc>https://example.test/{p}.html</loc></url>" for p in pages)
    (build / "sitemap.xml").write_text(
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>https://example.test/</loc></url>{locs}</urlset>",
        encoding="utf-8",
    )
    return build


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Fake cPanel home with an existing live site in public_html."""
    root = tmp_path / "home"
    (root / "public_html").mkdir(parents=True)
    (root / "public_html" / "index.html").write_text("<h1>OLD SITE</h1>", encoding="utf-8")
    (root / "public_html" / ".htaccess").write_text("RewriteEngine On", encoding="utf-8")
    return root


@pytest.fixture
def app(home: Path):
    return create_app(home, USER, TOKEN)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _ticking_clock(start: int):
    """Each deploy gets a distinct timestamp, as it would with a real clock."""
    state = {"t": start}

    def clock() -> int:
        state["t"] += 60
        return state["t"]

    return clock


@pytest.fixture
def deployer(client: TestClient, tmp_path: Path) -> Deployer:
    return Deployer(
        cp=CPanelClient(client, USER, TOKEN),
        site_http=client,
        site_base="http://testserver/site",
        blob_dir=tmp_path / "blob",
        site_id="site-1",
        clock=_ticking_clock(1_700_000_000),
    )


@pytest.fixture
def build(tmp_path: Path) -> Path:
    return write_build(tmp_path, "v1")
