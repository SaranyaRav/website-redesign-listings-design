"""
Mock cPanel UAPI server.

Mimics the handful of UAPI ``Fileman`` calls the deployer uses, over a local
directory that stands in for the account's home directory:

    GET  /execute/Fileman/list_files?dir=<path>
    POST /execute/Fileman/upload_files            (form: dir, file-1)
    GET  /execute/Fileman/get_file_content?dir=<path>&file=<name>
    GET  /execute/Fileman/mkdir?path=<path>&name=<name>
    GET  /execute/Fileman/fileop?op=rename|copy|unlink&sourcefiles=<p>&destfiles=<p>

Responses follow the real UAPI envelope::

    {"apiversion": 3, "module": "Fileman", "func": "...",
     "result": {"data": ..., "status": 1, "errors": null, "messages": null, "metadata": {}, "warnings": null}}

Authentication is the real cPanel API-token scheme:
``Authorization: cpanel <user>:<token>``.

Two things are NOT UAPI and exist only so the prototype is self-contained:

* ``/site/...`` serves ``public_html`` over HTTP (stands in for Apache on the
  client's domain) so post-deploy health checks have something to hit.
  A POST to a ``*.php`` path pretends PHP ran and returns JSON.
* ``app.state.faults`` lets tests inject failures (corrupted uploads, failing
  renames, 500s on served paths).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

MODULE = "Fileman"


def _envelope(func: str, data: Any = None, *, status: int = 1, errors: list[str] | None = None) -> dict:
    return {
        "apiversion": 3,
        "module": MODULE,
        "func": func,
        "result": {
            "data": data,
            "errors": errors,
            "messages": None,
            "metadata": {},
            "status": status,
            "warnings": None,
        },
    }


def create_app(root: Path, user: str, token: str) -> FastAPI:
    """Build a mock cPanel bound to ``root`` (the fake home directory)."""
    root = Path(root).resolve()
    (root / "public_html").mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="mock cPanel UAPI", docs_url=None, redoc_url=None)
    app.state.root = root
    app.state.faults: dict[str, Any] = {
        "corrupt_upload": False,        # append junk to every uploaded file
        "fail_rename_src": None,        # fileop rename fails when sourcefiles == this
        "serve_500_paths": set(),       # /site/<path> returns 500 for these paths
    }

    # ---------------------------------------------------------------- helpers
    def resolve(p: str) -> Path:
        candidate = (root / p.strip("/")).resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=400, detail="path escapes home directory")
        return candidate

    def require_token(request: Request) -> None:
        if request.headers.get("authorization", "") != f"cpanel {user}:{token}":
            raise HTTPException(status_code=403, detail="access denied: invalid API token")

    auth = Depends(require_token)

    # ------------------------------------------------------------------ UAPI
    @app.get("/execute/Fileman/list_files", dependencies=[auth])
    def list_files(dir: str = "/") -> dict:
        target = resolve(dir)
        if not target.exists():
            return _envelope("list_files", status=0, errors=[f"The directory {dir} does not exist."])
        items = []
        for child in sorted(target.iterdir()):
            items.append(
                {
                    "file": child.name,
                    "fullpath": str(child.relative_to(root)),
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else 0,
                }
            )
        return _envelope("list_files", items)

    @app.post("/execute/Fileman/upload_files", dependencies=[auth])
    async def upload_files(dir: str = Form(...), file: UploadFile = File(..., alias="file-1")) -> dict:
        target_dir = resolve(dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        data = await file.read()
        if app.state.faults["corrupt_upload"]:
            data += b"\n<!-- corrupted in transit -->\n"
        (target_dir / file.filename).write_bytes(data)
        return _envelope(
            "upload_files",
            {"succeeded": 1, "failed": 0, "warned": 0, "uploads": [{"file": file.filename, "status": 1, "reason": "OK"}]},
        )

    @app.get("/execute/Fileman/get_file_content", dependencies=[auth])
    def get_file_content(dir: str, file: str) -> dict:
        target = resolve(f"{dir}/{file}")
        if not target.is_file():
            return _envelope("get_file_content", status=0, errors=[f"The file {dir}/{file} does not exist."])
        return _envelope("get_file_content", {"content": target.read_text(encoding="utf-8"), "path": f"{dir}/{file}"})

    @app.get("/execute/Fileman/mkdir", dependencies=[auth])
    def mkdir(path: str, name: str) -> dict:
        resolve(f"{path}/{name}").mkdir(parents=True, exist_ok=True)
        return _envelope("mkdir", {"name": name, "path": path, "permissions": "0755"})

    @app.get("/execute/Fileman/fileop", dependencies=[auth])
    def fileop(op: str, sourcefiles: str, destfiles: str | None = None) -> dict:
        src = resolve(sourcefiles)
        if op == "rename":
            if app.state.faults["fail_rename_src"] == sourcefiles:
                return _envelope("fileop", status=0, errors=[f"Permission denied: cannot rename {sourcefiles}"])
            if not src.exists():
                return _envelope("fileop", status=0, errors=[f"{sourcefiles} does not exist."])
            dst = resolve(destfiles or "")
            if dst.exists():
                return _envelope("fileop", status=0, errors=[f"{destfiles} already exists."])
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return _envelope("fileop", [{"src": sourcefiles, "dest": destfiles, "result": 1}])
        if op == "copy":
            dst = resolve(destfiles or "")
            if dst.exists():
                return _envelope("fileop", status=0, errors=[f"{destfiles} already exists."])
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            return _envelope("fileop", [{"src": sourcefiles, "dest": destfiles, "result": 1}])
        if op == "unlink":
            if src.is_dir():
                shutil.rmtree(src)
            elif src.exists():
                src.unlink()
            return _envelope("fileop", [{"src": sourcefiles, "result": 1}])
        return _envelope("fileop", status=0, errors=[f"Unknown op {op}"])

    # --------------------------------------------------- stand-in for Apache
    @app.get("/site/{path:path}")
    def serve(path: str) -> Response:
        if path in app.state.faults["serve_500_paths"]:
            return JSONResponse({"error": "injected failure"}, status_code=500)
        target = resolve(f"public_html/{path}")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        media = "text/html" if target.suffix in {".html", ".php"} else "application/octet-stream"
        if target.suffix == ".css":
            media = "text/css"
        headers = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN"}
        return Response(content=target.read_bytes(), media_type=media, headers=headers)

    @app.post("/site/{path:path}")
    async def serve_php(path: str, request: Request) -> Response:
        """Pretend PHP executed the contact form; echo the test flag back."""
        target = resolve(f"public_html/{path}")
        if not path.endswith(".php") or not target.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        form = await request.form()
        return JSONResponse({"ok": True, "test": form.get("_test") == "1"})

    return app


def main() -> None:  # pragma: no cover - manual use
    """Run the mock on :2083 with a home dir and token taken from the environment."""
    import os

    import uvicorn

    root = Path(os.environ.get("MOCK_CPANEL_ROOT", "./.mock_home"))
    user = os.environ.get("CPANEL_USER", "demo")
    token = os.environ.get("CPANEL_API_TOKEN")
    if not token:
        raise SystemExit("Set CPANEL_API_TOKEN in the environment (never in a file in the repo).")
    uvicorn.run(create_app(root, user, token), host="127.0.0.1", port=int(os.environ.get("MOCK_CPANEL_PORT", "2083")))


if __name__ == "__main__":  # pragma: no cover
    main()
