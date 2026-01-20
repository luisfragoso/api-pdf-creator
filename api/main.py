import os
import asyncio
import ipaddress
import json
import logging
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


app = FastAPI(title="API PDF Creator")


PDF_API_KEYS = os.getenv("PDF_API_KEYS", "").strip()
PDF_REMOTE_API_KEYS = os.getenv("PDF_REMOTE_API_KEYS", "").strip()
ALLOWED_REMOTE_HOSTS = os.getenv("ALLOWED_REMOTE_HOSTS", "").strip()
MAX_HTML_BYTES = int(os.getenv("MAX_HTML_BYTES", "2000000"))
RENDER_TIMEOUT_SECONDS = float(os.getenv("RENDER_TIMEOUT_SECONDS", "90"))
WORKERS = int(os.getenv("WORKERS", "2"))

PDF_VIEWPORT_WIDTH = int(os.getenv("PDF_VIEWPORT_WIDTH", "1280"))
PDF_VIEWPORT_HEIGHT = int(os.getenv("PDF_VIEWPORT_HEIGHT", "720"))
PDF_OUTPUT_WIDTH = os.getenv("PDF_OUTPUT_WIDTH", "")
PDF_OUTPUT_HEIGHT = os.getenv("PDF_OUTPUT_HEIGHT", "")
PDF_MAX_HEIGHT_PX = int(os.getenv("PDF_MAX_HEIGHT_PX", "20000"))
PDF_SCREEN_SINGLE_PAGE = os.getenv("PDF_SCREEN_SINGLE_PAGE", "").strip().lower() in {"1", "true", "yes"}

RATE_LIMIT_NORMAL_PER_MIN = int(os.getenv("RATE_LIMIT_NORMAL_PER_MIN", "60"))
RATE_LIMIT_REMOTE_PER_MIN = int(os.getenv("RATE_LIMIT_REMOTE_PER_MIN", "20"))

JOBS_TTL_SECONDS = int(os.getenv("JOBS_TTL_SECONDS", "900"))


def _parse_csv_set(value: str) -> set[str]:
    if not value:
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


API_KEYS = _parse_csv_set(PDF_API_KEYS)
REMOTE_API_KEYS = _parse_csv_set(PDF_REMOTE_API_KEYS)
REMOTE_ALLOWED_HOSTS = {h.lower() for h in _parse_csv_set(ALLOWED_REMOTE_HOSTS)}


class _AuthContext(BaseModel):
    token: str
    allow_remote: bool


_logger = logging.getLogger("pdf_api")
if not _logger.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


_executor = ThreadPoolExecutor(max_workers=max(1, WORKERS))

_chromium_lock = threading.RLock()
_playwright = None
_chromium_browser = None


def _get_chromium_browser():
    global _playwright, _chromium_browser
    if sync_playwright is None:
        raise HTTPException(status_code=500, detail="Chromium no disponible (Playwright no instalado)")

    if _chromium_browser is not None:
        return _chromium_browser

    with _chromium_lock:
        if _chromium_browser is not None:
            return _chromium_browser

        _logger.info(json.dumps({"event": "chromium_init", "stage": "start_playwright"}))
        _playwright = sync_playwright().start()
        _logger.info(json.dumps({"event": "chromium_init", "stage": "launch"}))
        _chromium_browser = _playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        _logger.info(json.dumps({"event": "chromium_init", "stage": "ready"}))
        return _chromium_browser


class _RateLimiter:
    def __init__(self):
        self._hits: dict[str, list[float]] = {}

    def check(self, key: str, limit_per_min: int) -> None:
        if limit_per_min <= 0:
            return

        now = time.time()
        window_start = now - 60.0
        hits = self._hits.get(key)
        if hits is None:
            hits = []
            self._hits[key] = hits

        while hits and hits[0] < window_start:
            hits.pop(0)

        if len(hits) >= limit_per_min:
            raise HTTPException(status_code=429, detail="Rate limit excedido")

        hits.append(now)


_rate_limiter = _RateLimiter()


class _JobStatus(BaseModel):
    id: str
    status: str
    created_at: float
    updated_at: float
    error: str | None = None


class _Job:
    def __init__(self, job_id: str):
        now = time.time()
        self.status = _JobStatus(id=job_id, status="queued", created_at=now, updated_at=now)
        self.future: Future[bytes] | None = None
        self.pdf_bytes: bytes | None = None


_jobs: dict[str, _Job] = {}


def _cleanup_jobs() -> None:
    if JOBS_TTL_SECONDS <= 0:
        return
    now = time.time()
    to_delete: list[str] = []
    for jid, job in _jobs.items():
        if now - job.status.updated_at > JOBS_TTL_SECONDS:
            to_delete.append(jid)
    for jid in to_delete:
        _jobs.pop(jid, None)


_bearer_scheme = HTTPBearer(auto_error=False)
_api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_api_key(
    bearer: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    x_api_key: str | None = Depends(_api_key_scheme),
):
    if not API_KEYS and not REMOTE_API_KEYS:
        return _AuthContext(token="", allow_remote=False)

    token = bearer.credentials.strip() if bearer and bearer.credentials else None

    if not token and x_api_key:
        token = x_api_key.strip()

    if not token:
        raise HTTPException(status_code=401, detail="No autorizado")

    _cleanup_jobs()

    if token in REMOTE_API_KEYS:
        _rate_limiter.check(f"remote:{token}", RATE_LIMIT_REMOTE_PER_MIN)
        return _AuthContext(token=token, allow_remote=True)

    if token in API_KEYS:
        _rate_limiter.check(f"normal:{token}", RATE_LIMIT_NORMAL_PER_MIN)
        return _AuthContext(token=token, allow_remote=False)

    raise HTTPException(status_code=401, detail="No autorizado")


def _enforce_size_limit(text: str):
    if MAX_HTML_BYTES > 0 and len(text.encode("utf-8")) > MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail="HTML demasiado grande")


def _is_forbidden_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True

    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _hostname_resolves_to_forbidden_ip(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return True

    for info in infos:
        ip = info[4][0]
        if _is_forbidden_ip(ip):
            return True
    return False


def _assert_remote_url_allowed(url: str, allow_remote: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return

    if not allow_remote:
        raise HTTPException(status_code=400, detail="Recursos remotos (http/https) no permitidos")

    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Solo se permiten recursos remotos HTTPS")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise HTTPException(status_code=400, detail="Host remoto inválido")

    if REMOTE_ALLOWED_HOSTS and host not in REMOTE_ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail="Host remoto no permitido")

    if host in {"localhost"}:
        raise HTTPException(status_code=400, detail="Host remoto no permitido")

    try:
        ipaddress.ip_address(host)
        if _is_forbidden_ip(host):
            raise HTTPException(status_code=400, detail="Host remoto no permitido")
    except ValueError:
        if _hostname_resolves_to_forbidden_ip(host):
            raise HTTPException(status_code=400, detail="Host remoto no permitido")


def _normalize_html_input(html: str) -> str:
    if not html:
        return html

    normalized = html
    if "\\n" in normalized or "\\t" in normalized or "\\r" in normalized:
        normalized = (
            normalized.replace("\\r\\n", "\n")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
        )

    return normalized


def _inject_base_href(html: str, base_url: str | None) -> str:
    if not base_url:
        return html

    lowered = html.lower()
    if "<base" in lowered:
        return html

    tag = f'<base href="{base_url}">'
    head_idx = lowered.find("<head")
    if head_idx != -1:
        head_close = lowered.find(">", head_idx)
        if head_close != -1:
            return html[: head_close + 1] + tag + html[head_close + 1 :]

    html_idx = lowered.find("<html")
    if html_idx != -1:
        html_close = lowered.find(">", html_idx)
        if html_close != -1:
            return html[: html_close + 1] + f"<head>{tag}</head>" + html[html_close + 1 :]

    return f"<head>{tag}</head>" + html


def _render_pdf_chromium(
    html: str,
    base_url: str | None,
    allow_remote: bool,
    media: str,
) -> bytes:
    normalized_html = _inject_base_href(html, base_url)
    t0 = time.perf_counter()

    _logger.info(
        json.dumps(
            {
                "event": "render_pdf_chromium_stage",
                "stage": "get_browser",
                "media": media,
                "allow_remote": allow_remote,
            }
        )
    )
    if sync_playwright is None:
        raise HTTPException(status_code=500, detail="Chromium no disponible (Playwright no instalado)")

    def _route_handler(route, request):
        url = request.url
        parsed = urlparse(url)

        if parsed.scheme in {"http", "https"}:
            try:
                _assert_remote_url_allowed(url, allow_remote=allow_remote)
            except HTTPException:
                _logger.info(
                    json.dumps(
                        {
                            "event": "render_pdf_chromium_resource_blocked",
                            "media": media,
                            "allow_remote": allow_remote,
                            "url": url,
                        }
                    )
                )
                route.abort()
                return
            route.continue_()
            return

        if parsed.scheme in {"file", "data", "blob", "about"}:
            route.continue_()
            return

        route.abort()

    t_pw0 = time.perf_counter()
    _logger.info(
        json.dumps(
            {
                "event": "render_pdf_chromium_stage",
                "stage": "playwright_start",
                "media": media,
                "allow_remote": allow_remote,
            }
        )
    )

    old_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _logger.info(
        json.dumps(
            {
                "event": "render_pdf_chromium_stage",
                "stage": "asyncio_policy_ready",
                "media": media,
                "allow_remote": allow_remote,
                "policy": old_policy.__class__.__name__,
            }
        )
    )

    try:
        with sync_playwright() as p:
            t_pw1 = time.perf_counter()
            _logger.info(
                json.dumps(
                    {
                        "event": "render_pdf_chromium_stage",
                        "stage": "playwright_ready",
                        "media": media,
                        "allow_remote": allow_remote,
                        "ms": int((t_pw1 - t_pw0) * 1000),
                    }
                )
            )

            t_launch0 = time.perf_counter()
            _logger.info(
                json.dumps(
                    {
                        "event": "render_pdf_chromium_stage",
                        "stage": "browser_launch_start",
                        "media": media,
                        "allow_remote": allow_remote,
                    }
                )
            )

            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            t_launch1 = time.perf_counter()
            _logger.info(
                json.dumps(
                    {
                        "event": "render_pdf_chromium_stage",
                        "stage": "browser_launch_ready",
                        "media": media,
                        "allow_remote": allow_remote,
                        "ms": int((t_launch1 - t_launch0) * 1000),
                    }
                )
            )

            page = None
            try:
                _logger.info(
                    json.dumps(
                        {
                            "event": "render_pdf_chromium_stage",
                            "stage": "new_page",
                            "media": media,
                            "allow_remote": allow_remote,
                        }
                    )
                )

                page = browser.new_page(viewport={"width": PDF_VIEWPORT_WIDTH, "height": PDF_VIEWPORT_HEIGHT})
                page.route("**/*", _route_handler)
                page.on(
                    "requestfailed",
                    lambda req: _logger.info(
                        json.dumps(
                            {
                                "event": "render_pdf_chromium_request_failed",
                                "media": media,
                                "allow_remote": allow_remote,
                                "url": req.url,
                                "failure": (req.failure or {}).get("errorText"),
                            }
                        )
                    ),
                )
                if media in {"screen", "print"}:
                    page.emulate_media(media=media)

                t_set_content0 = time.perf_counter()
                page.set_content(
                    normalized_html,
                    wait_until="domcontentloaded",
                    timeout=int(RENDER_TIMEOUT_SECONDS * 1000),
                )

                t_after_content = time.perf_counter()
                _logger.info(
                    json.dumps(
                        {
                            "event": "render_pdf_chromium_stage",
                            "stage": "content_set",
                            "media": media,
                            "allow_remote": allow_remote,
                            "ms": int((t_after_content - t_set_content0) * 1000),
                        }
                    )
                )
                try:
                    page.wait_for_load_state("load", timeout=2000)
                except Exception:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:
                    pass
                try:
                    page.wait_for_function(
                        "document.fonts && document.fonts.status === 'loaded'",
                        timeout=2000,
                    )
                except Exception:
                    pass
                try:
                    page.wait_for_function(
                        "Array.from(document.images || []).every(i => i.complete)",
                        timeout=5000,
                    )
                except Exception:
                    pass
                page.wait_for_timeout(100)

                t_ready = time.perf_counter()
                _logger.info(
                    json.dumps(
                        {
                            "event": "render_pdf_chromium_stage",
                            "stage": "ready",
                            "media": media,
                            "allow_remote": allow_remote,
                            "ms": int((t_ready - t_after_content) * 1000),
                        }
                    )
                )

                pdf_options: dict[str, object] = {
                    "print_background": True,
                    "prefer_css_page_size": True,
                    "margin": {"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    "scale": 1,
                }

                if media == "screen":
                    if PDF_SCREEN_SINGLE_PAGE or PDF_OUTPUT_WIDTH or PDF_OUTPUT_HEIGHT:
                        out_w = (PDF_OUTPUT_WIDTH or f"{PDF_VIEWPORT_WIDTH}px").strip()
                        if PDF_OUTPUT_HEIGHT:
                            out_h = PDF_OUTPUT_HEIGHT.strip()
                        else:
                            scroll_h = page.evaluate(
                                "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
                            )
                            try:
                                scroll_h_int = int(scroll_h)
                            except Exception:
                                scroll_h_int = PDF_VIEWPORT_HEIGHT
                            height_px = max(scroll_h_int, PDF_VIEWPORT_HEIGHT)
                            if PDF_MAX_HEIGHT_PX > 0:
                                height_px = min(height_px, PDF_MAX_HEIGHT_PX)
                            out_h = f"{height_px}px"
                        pdf_options["width"] = out_w
                        pdf_options["height"] = out_h
                        pdf_options["prefer_css_page_size"] = False
                    else:
                        # Mantener el layout "screen" (sin reflow) pero paginar:
                        # usa el mismo ancho del viewport y un alto fijo tipo hoja.
                        # (A4 aprox. a 96dpi => 1123px) para evitar saltos raros con secciones 100vh.
                        pdf_options["width"] = (PDF_OUTPUT_WIDTH or f"{PDF_VIEWPORT_WIDTH}px").strip()
                        pdf_options["height"] = (PDF_OUTPUT_HEIGHT or "1123px").strip()
                        pdf_options["prefer_css_page_size"] = False

                t_pdf0 = time.perf_counter()
                _logger.info(
                    json.dumps(
                        {
                            "event": "render_pdf_chromium_stage",
                            "stage": "pdf_start",
                            "media": media,
                            "allow_remote": allow_remote,
                        }
                    )
                )

                pdf = page.pdf(**pdf_options)
                t_pdf1 = time.perf_counter()
                _logger.info(
                    json.dumps(
                        {
                            "event": "render_pdf_chromium",
                            "media": media,
                            "allow_remote": allow_remote,
                            "ms_set_content": int((t_after_content - t_set_content0) * 1000),
                            "ms_ready": int((t_ready - t_after_content) * 1000),
                            "ms_pdf": int((t_pdf1 - t_pdf0) * 1000),
                            "ms_total": int((t_pdf1 - t0) * 1000),
                        }
                    )
                )
                return pdf
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                try:
                    browser.close()
                except Exception:
                    pass
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop_policy(old_policy)


@app.on_event("shutdown")
def _shutdown_playwright():
    global _playwright, _chromium_browser
    with _chromium_lock:
        if _chromium_browser is not None:
            try:
                _chromium_browser.close()
            except Exception:
                pass
            _chromium_browser = None
        if _playwright is not None:
            try:
                _playwright.stop()
            except Exception:
                pass
            _playwright = None


def _render_pdf_with_timeout(
    html: str,
    base_url: str | None,
    allow_remote: bool,
    media: str,
) -> bytes:
    future = _executor.submit(
        _render_pdf_chromium,
        html=html,
        base_url=base_url,
        allow_remote=allow_remote,
        media=media,
    )
    try:
        return future.result(timeout=RENDER_TIMEOUT_SECONDS + 15.0)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Timeout generando PDF") from exc


class PdfRequest(BaseModel):
    html: str
    filename: str | None = "documento.pdf"
    base_url: str | None = None
    media: str | None = None


def _default_media() -> str:
    return "screen"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {
        "service": "html-to-pdf-api",
        "playwright_available": sync_playwright is not None,
        "max_html_bytes": MAX_HTML_BYTES,
        "render_timeout_seconds": RENDER_TIMEOUT_SECONDS,
        "pdf_viewport_width": PDF_VIEWPORT_WIDTH,
        "pdf_viewport_height": PDF_VIEWPORT_HEIGHT,
        "pdf_output_width": PDF_OUTPUT_WIDTH,
        "pdf_output_height": PDF_OUTPUT_HEIGHT,
        "pdf_max_height_px": PDF_MAX_HEIGHT_PX,
        "pdf_screen_single_page": PDF_SCREEN_SINGLE_PAGE,
        "rate_limit_normal_per_min": RATE_LIMIT_NORMAL_PER_MIN,
        "rate_limit_remote_per_min": RATE_LIMIT_REMOTE_PER_MIN,
        "jobs_ttl_seconds": JOBS_TTL_SECONDS,
    }


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    req_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    started = time.time()
    response: Response
    try:
        response = await call_next(request)
    finally:
        elapsed_ms = int((time.time() - started) * 1000)
        _logger.info(
            json.dumps(
                {
                    "request_id": req_id,
                    "method": request.method,
                    "path": request.url.path,
                    "elapsed_ms": elapsed_ms,
                }
            )
        )

    response.headers["X-Request-Id"] = req_id
    return response


@app.post(
    "/pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def create_pdf(payload: PdfRequest, auth: _AuthContext = Depends(_require_api_key)):
    if not payload.html or not payload.html.strip():
        raise HTTPException(status_code=400, detail="El campo 'html' es requerido")

    html = _normalize_html_input(payload.html)
    _enforce_size_limit(html)

    media = (payload.media or _default_media()).strip().lower()
    if media not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    try:
        started = time.time()
        pdf_bytes = _render_pdf_with_timeout(
            html=html,
            base_url=payload.base_url,
            allow_remote=auth.allow_remote,
            media=media,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        _logger.info(
            json.dumps(
                {
                    "event": "render_pdf",
                    "token_type": "remote" if auth.allow_remote else "normal",
                    "html_bytes": len(html.encode("utf-8")),
                    "elapsed_ms": elapsed_ms,
                }
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo generar el PDF: {exc}")

    filename = payload.filename or "documento.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post(
    "/pdf/raw",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
def create_pdf_raw(
    html: str = Body(..., media_type="text/html"),
    filename: str | None = Query("documento.pdf"),
    base_url: str | None = Query(None),
    media: str | None = Query(None),
    auth: _AuthContext = Depends(_require_api_key),
):
    if not html or not html.strip():
        raise HTTPException(status_code=400, detail="El cuerpo HTML es requerido")

    normalized_html = _normalize_html_input(html)
    _enforce_size_limit(normalized_html)

    media_normalized = (media or _default_media()).strip().lower()
    if media_normalized not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    try:
        started = time.time()
        pdf_bytes = _render_pdf_with_timeout(
            html=normalized_html,
            base_url=base_url,
            allow_remote=auth.allow_remote,
            media=media_normalized,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        _logger.info(
            json.dumps(
                {
                    "event": "render_pdf",
                    "token_type": "remote" if auth.allow_remote else "normal",
                    "html_bytes": len(normalized_html.encode("utf-8")),
                    "elapsed_ms": elapsed_ms,
                }
            )
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo generar el PDF: {exc}")

    safe_filename = filename or "documento.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )


@app.post("/jobs")
def create_job(payload: PdfRequest, auth: _AuthContext = Depends(_require_api_key)):
    html = _normalize_html_input(payload.html)
    _enforce_size_limit(html)

    media = (payload.media or _default_media()).strip().lower()
    if media not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    job_id = str(uuid.uuid4())
    job = _Job(job_id)
    _jobs[job_id] = job

    def _run():
        job.status.status = "running"
        job.status.updated_at = time.time()
        result = _render_pdf_with_timeout(
            html=html,
            base_url=payload.base_url,
            allow_remote=auth.allow_remote,
            media=media,
        )
        return result

    future = _executor.submit(_run)
    job.future = future

    def _done(f: Future[bytes]):
        try:
            job.pdf_bytes = f.result()
            job.status.status = "done"
        except Exception as exc:
            job.status.status = "error"
            job.status.error = str(exc)
        finally:
            job.status.updated_at = time.time()

    future.add_done_callback(_done)

    return {"job_id": job_id, "status": job.status.status}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, _: _AuthContext = Depends(_require_api_key)):
    _cleanup_jobs()
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    return job.status.model_dump()


@app.get("/jobs/{job_id}/pdf")
def download_job_pdf(job_id: str, _: _AuthContext = Depends(_require_api_key)):
    _cleanup_jobs()
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado")
    if job.status.status != "done" or not job.pdf_bytes:
        raise HTTPException(status_code=409, detail="PDF no disponible")
    return Response(content=job.pdf_bytes, media_type="application/pdf")


@app.post(
    "/pdf/upload",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def create_pdf_upload(
    html: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    filename: str | None = Query("documento.pdf"),
    media: str | None = Query(None),
    auth: _AuthContext = Depends(_require_api_key),
):
    if not html or not html.strip():
        raise HTTPException(status_code=400, detail="El campo 'html' es requerido")

    normalized_html = _normalize_html_input(html)
    _enforce_size_limit(normalized_html)

    media_normalized = (media or _default_media()).strip().lower()
    if media_normalized not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    safe_filename = filename or "documento.pdf"

    with tempfile.TemporaryDirectory(prefix="pdf-assets-") as tmpdir:
        base_path = Path(tmpdir)

        for f in files:
            if not f.filename:
                continue

            target = base_path / Path(f.filename).name
            data = await f.read()
            target.write_bytes(data)

        try:
            started = time.time()
            pdf_bytes = _render_pdf_with_timeout(
                html=normalized_html,
                base_url=base_path.as_uri() + "/",
                allow_remote=auth.allow_remote,
                media=media_normalized,
            )
            elapsed_ms = int((time.time() - started) * 1000)
            _logger.info(
                json.dumps(
                    {
                        "event": "render_pdf",
                        "token_type": "remote" if auth.allow_remote else "normal",
                        "html_bytes": len(normalized_html.encode("utf-8")),
                        "elapsed_ms": elapsed_ms,
                    }
                )
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"No se pudo generar el PDF: {exc}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )
