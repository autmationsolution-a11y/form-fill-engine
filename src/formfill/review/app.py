"""Human-in-the-loop review app.

A reviewer sees each filled field side by side with its source value, opens the actual PDF,
and approves or rejects. Approval is the only path to the outbox.

Identity: by default the reviewer types their name. In a real deployment, run the app behind
a reverse proxy that does single sign-on and pass ``trusted_user_header`` (for example
``X-Forwarded-User``). The app then takes identity from that header only and ignores the form.
Bind to localhost unless such a proxy is in front of it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..pdf import read_values
from .store import PENDING, PackageStore, ReviewError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _display_pdf_value(kind: str, value: str) -> str:
    if kind == "checkbox":
        return "unchecked" if value in ("", "Off") else "checked"
    return value


def _expected_display(field: dict[str, Any]) -> str:
    if field["status"] not in ("filled", "doubtful"):
        return ""
    value = field["output_value"]
    if field["type"] == "checkbox":
        return "checked" if value else "unchecked"
    return "" if value is None else str(value)


def build_rows(package_report: dict[str, Any], pdf_values: dict[str, str]) -> list[dict[str, Any]]:
    """Pair each mapped field with what is really in the PDF, and flag any disagreement."""
    rows = []
    for field in package_report["fields"]:
        in_pdf = _display_pdf_value(field["type"], pdf_values.get(field["pdf_field"], ""))
        expected = _expected_display(field)
        blank_ok = field["type"] == "checkbox" and expected == "" and in_pdf == "unchecked"
        rows.append(
            {
                **field,
                "in_pdf": in_pdf,
                "mismatch": in_pdf != expected and not blank_ok,
            }
        )
    return rows


def create_app(workdir: Path | str, trusted_user_header: str | None = None) -> FastAPI:
    store = PackageStore(workdir)
    app = FastAPI(title="form-fill-engine review", docs_url=None, redoc_url=None)
    app.state.store = store

    def actor(request: Request, typed: str) -> str:
        if trusted_user_header:
            value = request.headers.get(trusted_user_header, "").strip()
            if not value:
                raise ReviewError("no authenticated user on the request", code=401)
            return value
        return typed

    @app.exception_handler(ReviewError)
    async def review_error(request: Request, exc: ReviewError):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc)}, status_code=exc.code)
        return TEMPLATES.TemplateResponse(
            request, "error.html", {"message": str(exc)}, status_code=exc.code
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        packages = store.list()
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {"packages": packages, "pending": sum(p.status == PENDING for p in packages)},
        )

    @app.get("/packages/{package_id}", response_class=HTMLResponse)
    def review(request: Request, package_id: str):
        pkg = store.get(package_id)
        rows = build_rows(pkg.report, read_values(pkg.pdf_path))
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            {
                "pkg": pkg,
                "rows": rows,
                "mismatches": sum(r["mismatch"] for r in rows),
                "sso": bool(trusted_user_header),
            },
        )

    @app.get("/packages/{package_id}/pdf")
    def pdf(package_id: str):
        pkg = store.get(package_id)
        return FileResponse(
            pkg.pdf_path,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{pkg.id}.pdf"'},
        )

    @app.post("/packages/{package_id}/approve")
    def approve(
        request: Request, package_id: str, reviewer: str = Form(""), comment: str = Form("")
    ):
        store.approve(package_id, actor(request, reviewer), comment)
        return RedirectResponse(f"/packages/{package_id}", status_code=303)

    @app.post("/packages/{package_id}/reject")
    def reject(request: Request, package_id: str, reviewer: str = Form(""), reason: str = Form("")):
        store.reject(package_id, actor(request, reviewer), reason)
        return RedirectResponse(f"/packages/{package_id}", status_code=303)

    @app.get("/api/packages")
    def api_packages(status: str | None = None):
        return [
            {
                "package_id": p.id,
                "status": p.status,
                "template_id": p.data["template_id"],
                "client_id": p.data["client_id"],
                "blocking": p.report["blocking"],
                "summary": p.report["summary"],
            }
            for p in store.list(status)
        ]

    @app.get("/api/audit/verify")
    def api_audit_verify():
        ok, message = store.audit.verify()
        return {"ok": ok, "message": message}

    return app
