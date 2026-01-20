import os
import asyncio
import re
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
from urllib.parse import unquote
from urllib.parse import urljoin
from pathlib import PurePosixPath
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
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
MAX_RENDER_QUEUE = int(os.getenv("MAX_RENDER_QUEUE", "0"))

CHROMIUM_PERSISTENT = os.getenv("CHROMIUM_PERSISTENT", "").strip().lower() in {"1", "true", "yes"}
CHROMIUM_RESTART_AFTER = int(os.getenv("CHROMIUM_RESTART_AFTER", "0"))
HEALTH_DEEP_TIMEOUT_SECONDS = float(os.getenv("HEALTH_DEEP_TIMEOUT_SECONDS", "15"))

PDF_VIEWPORT_WIDTH = int(os.getenv("PDF_VIEWPORT_WIDTH", "1280"))
PDF_VIEWPORT_HEIGHT = int(os.getenv("PDF_VIEWPORT_HEIGHT", "720"))
PDF_OUTPUT_WIDTH = os.getenv("PDF_OUTPUT_WIDTH", "")
PDF_OUTPUT_HEIGHT = os.getenv("PDF_OUTPUT_HEIGHT", "")
PDF_MAX_HEIGHT_PX = int(os.getenv("PDF_MAX_HEIGHT_PX", "20000"))
PDF_SCREEN_SINGLE_PAGE = os.getenv("PDF_SCREEN_SINGLE_PAGE", "").strip().lower() in {"1", "true", "yes"}

RATE_LIMIT_NORMAL_PER_MIN = int(os.getenv("RATE_LIMIT_NORMAL_PER_MIN", "60"))
RATE_LIMIT_REMOTE_PER_MIN = int(os.getenv("RATE_LIMIT_REMOTE_PER_MIN", "20"))

JOBS_TTL_SECONDS = int(os.getenv("JOBS_TTL_SECONDS", "900"))

ALLOW_ANONYMOUS = os.getenv("ALLOW_ANONYMOUS", "").strip().lower() in {"1", "true", "yes"}
ALLOW_FILE_SCHEME = os.getenv("ALLOW_FILE_SCHEME", "").strip().lower() in {"1", "true", "yes"}
EXTRA_HEADERS_REMOTE_ONLY = os.getenv("EXTRA_HEADERS_REMOTE_ONLY", "1").strip().lower() in {"1", "true", "yes"}
BLOCKED_EXTRA_HEADERS = os.getenv("BLOCKED_EXTRA_HEADERS", "authorization,cookie,proxy-authorization").strip()
MAX_EXTRA_HEADERS = int(os.getenv("MAX_EXTRA_HEADERS", "20"))
MAX_HEADER_VALUE_LEN = int(os.getenv("MAX_HEADER_VALUE_LEN", "1024"))


def _parse_csv_set(value: str) -> set[str]:
    if not value:
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


API_KEYS = _parse_csv_set(PDF_API_KEYS)
REMOTE_API_KEYS = _parse_csv_set(PDF_REMOTE_API_KEYS)
REMOTE_ALLOWED_HOSTS = {h.lower() for h in _parse_csv_set(ALLOWED_REMOTE_HOSTS)}

ALLOWED_REMOTE_URL_REGEX = os.getenv("ALLOWED_REMOTE_URL_REGEX", "").strip()
DENIED_REMOTE_URL_REGEX = os.getenv("DENIED_REMOTE_URL_REGEX", "").strip()
REMOTE_STRICT_RESOURCES = os.getenv("REMOTE_STRICT_RESOURCES", "").strip().lower() in {"1", "true", "yes"}
RESOURCE_STATUS_IGNORE_DOMAINS = os.getenv("RESOURCE_STATUS_IGNORE_DOMAINS", "").strip()
FORCE_EXACT_COLORS = os.getenv("FORCE_EXACT_COLORS", "").strip().lower() in {"1", "true", "yes"}

_ALLOWED_REMOTE_URL_RE = re.compile(ALLOWED_REMOTE_URL_REGEX, re.IGNORECASE) if ALLOWED_REMOTE_URL_REGEX else None
_DENIED_REMOTE_URL_RE = re.compile(DENIED_REMOTE_URL_REGEX, re.IGNORECASE) if DENIED_REMOTE_URL_REGEX else None
_RESOURCE_STATUS_IGNORE_DOMAINS = {h.lower() for h in _parse_csv_set(RESOURCE_STATUS_IGNORE_DOMAINS)}
_BLOCKED_EXTRA_HEADERS = {h.lower() for h in _parse_csv_set(BLOCKED_EXTRA_HEADERS)}


class _AuthContext(BaseModel):
    token: str
    allow_remote: bool


_logger = logging.getLogger("pdf_api")
if not _logger.handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


_executor = ThreadPoolExecutor(max_workers=max(1, WORKERS))
_health_executor = ThreadPoolExecutor(max_workers=1)

_render_slots = threading.BoundedSemaphore(
    value=max(1, WORKERS) + max(0, MAX_RENDER_QUEUE) if MAX_RENDER_QUEUE > 0 else 10**9
)
_render_inflight = 0
_render_inflight_lock = threading.Lock()

_chromium_lock = threading.RLock()
_playwright = None
_chromium_browser = None

_chromium_render_count = 0


def _get_chromium_browser():
    global _playwright, _chromium_browser
    if sync_playwright is None:
        raise HTTPException(status_code=500, detail="Chromium no disponible (Playwright no instalado)")

    if _chromium_browser is not None:
        return _chromium_browser

    with _chromium_lock:
        if _chromium_browser is not None:
            return _chromium_browser

        old_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            _logger.info(json.dumps({"event": "chromium_init", "stage": "start_playwright"}))
            _playwright = sync_playwright().start()
            _logger.info(json.dumps({"event": "chromium_init", "stage": "launch"}))
            _chromium_browser = _playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--no-zygote",
                    "--disable-pdf-tagging",
                    "--font-render-hinting=none",
                ],
            )
            _logger.info(json.dumps({"event": "chromium_init", "stage": "ready"}))
            return _chromium_browser
        finally:
            try:
                loop.close()
            except Exception:
                pass
            asyncio.set_event_loop_policy(old_policy)


def _maybe_restart_persistent_browser():
    global _chromium_render_count, _playwright, _chromium_browser
    if not CHROMIUM_PERSISTENT:
        return
    if CHROMIUM_RESTART_AFTER <= 0:
        return
    if _chromium_render_count < CHROMIUM_RESTART_AFTER:
        return

    with _chromium_lock:
        if _chromium_render_count < CHROMIUM_RESTART_AFTER:
            return
        _logger.info(json.dumps({"event": "chromium_restart", "reason": "restart_after", "count": _chromium_render_count}))
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
        _chromium_render_count = 0


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
        if ALLOW_ANONYMOUS:
            return _AuthContext(token="", allow_remote=False)
        raise HTTPException(status_code=500, detail="API keys no configuradas")

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

    if _DENIED_REMOTE_URL_RE and _DENIED_REMOTE_URL_RE.search(url):
        raise HTTPException(status_code=400, detail="URL remota no permitida")

    if _ALLOWED_REMOTE_URL_RE and not _ALLOWED_REMOTE_URL_RE.search(url):
        raise HTTPException(status_code=400, detail="URL remota no permitida")

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


def _is_path_within_root(path: str, root: str) -> bool:
    try:
        p = os.path.realpath(path)
        r = os.path.realpath(root)
    except Exception:
        return False

    try:
        common = os.path.commonpath([p, r])
    except Exception:
        return False
    return common == r


def _validate_base_url(
    base_url: str | None,
    *,
    allow_remote: bool,
    allow_file_access: bool,
    file_root: str | None,
) -> None:
    if not base_url:
        return
    parsed = urlparse(base_url)

    if parsed.scheme == "file":
        if not allow_file_access:
            raise HTTPException(status_code=400, detail="base_url file:// no permitido")
        if not ALLOW_FILE_SCHEME:
            raise HTTPException(status_code=400, detail="base_url file:// no permitido")
        if not file_root:
            raise HTTPException(status_code=400, detail="base_url file:// no permitido")
        file_path = unquote(parsed.path or "")
        if not file_path:
            raise HTTPException(status_code=400, detail="base_url inválido")
        if not _is_path_within_root(file_path, file_root):
            raise HTTPException(status_code=400, detail="base_url file:// fuera de directorio permitido")
        return

    if parsed.scheme in {"http", "https"}:
        _assert_remote_url_allowed(base_url, allow_remote=allow_remote)
        return

    if parsed.scheme:
        raise HTTPException(status_code=400, detail="base_url inválido")


def _sanitize_extra_http_headers(headers: dict[str, str] | None, allow_remote: bool) -> dict[str, str] | None:
    if not headers:
        return None
    if EXTRA_HEADERS_REMOTE_ONLY and not allow_remote:
        raise HTTPException(status_code=400, detail="extra_http_headers no permitido")

    if len(headers) > MAX_EXTRA_HEADERS:
        raise HTTPException(status_code=400, detail="Demasiados headers extra")

    sanitized: dict[str, str] = {}
    for k, v in headers.items():
        key = str(k).strip()
        if not key:
            continue
        key_lower = key.lower()
        if key_lower in _BLOCKED_EXTRA_HEADERS:
            raise HTTPException(status_code=400, detail=f"Header no permitido: {key}")
        value = str(v)
        if len(value) > MAX_HEADER_VALUE_LEN:
            raise HTTPException(status_code=400, detail=f"Header demasiado largo: {key}")
        sanitized[key] = value
    return sanitized or None


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


def _html_defines_page_size(html: str) -> bool:
    if not html:
        return False
    lowered = html.lower()
    lowered = re.sub(r"/\*[\s\S]*?\*/", "", lowered)
    if "@page" not in lowered:
        return False
    return bool(re.search(r"@page\b[\s\S]{0,2000}\bsize\s*:", lowered))


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
    wait_delay_ms: int,
    wait_for_selector: str | None,
    wait_for_expression: str | None,
    wait_window_status: str | None,
    wait_networkidle: bool,
    strict_resources: bool,
    user_agent: str | None,
    extra_http_headers: dict[str, str] | None,
    force_exact_colors: bool,
    allow_file_access: bool,
    file_root: str | None,
) -> bytes:
    normalized_html = _inject_base_href(html, base_url)
    t0 = time.perf_counter()

    blocked_urls: list[dict[str, object]] = []

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

    def _render_with_browser(
        *,
        browser,
        normalized_html: str,
        media: str,
        allow_remote: bool,
        wait_delay_ms: int,
        wait_for_selector: str | None,
        wait_for_expression: str | None,
        wait_window_status: str | None,
        wait_networkidle: bool,
        strict_resources: bool,
        user_agent: str | None,
        extra_http_headers: dict[str, str] | None,
        force_exact_colors: bool,
        allow_file_access: bool,
        file_root: str | None,
        route_handler,
        t0: float,
    ) -> bytes:
        context = None
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

            context = browser.new_context(
                viewport={"width": PDF_VIEWPORT_WIDTH, "height": PDF_VIEWPORT_HEIGHT},
                user_agent=user_agent or None,
            )
            if extra_http_headers:
                context.set_extra_http_headers(extra_http_headers)
            page = context.new_page()
            page.route("**/*", route_handler)

            resource_failures: list[dict[str, str | None]] = []
            resource_bad_status: list[dict[str, object]] = []

            def _failure_text(req) -> str | None:
                try:
                    f = req.failure
                except Exception:
                    return None
                if f is None:
                    return None
                if isinstance(f, dict):
                    return str(f.get("errorText") or f.get("error"))
                return str(f)

            page.on(
                "requestfailed",
                lambda req: _logger.info(
                    json.dumps(
                        {
                            "event": "render_pdf_chromium_request_failed",
                            "media": media,
                            "allow_remote": allow_remote,
                            "url": req.url,
                            "failure": _failure_text(req),
                        }
                    )
                ),
            )
            page.on(
                "requestfailed",
                lambda req: resource_failures.append(
                    {
                        "url": req.url,
                        "failure": _failure_text(req),
                    }
                ),
            )
            def _on_response(resp):
                try:
                    if (
                        strict_resources
                        and resp.status >= 400
                        and (
                            not _RESOURCE_STATUS_IGNORE_DOMAINS
                            or (urlparse(resp.url).hostname or "").lower() not in _RESOURCE_STATUS_IGNORE_DOMAINS
                        )
                    ):
                        resource_bad_status.append(
                            {
                                "url": resp.url,
                                "status": resp.status,
                            }
                        )

                    if 300 <= resp.status < 400:
                        location = None
                        try:
                            location = (resp.headers or {}).get("location")
                        except Exception:
                            location = None
                        if location:
                            target = urljoin(resp.url, location)
                            t_parsed = urlparse(target)

                            if t_parsed.scheme in {"http", "https"}:
                                try:
                                    _assert_remote_url_allowed(target, allow_remote=allow_remote)
                                except HTTPException:
                                    blocked_urls.append(
                                        {
                                            "url": target,
                                            "reason": "redirect_target_not_allowed",
                                            "from": resp.url,
                                        }
                                    )
                            elif t_parsed.scheme == "file":
                                file_path = unquote(t_parsed.path or "")
                                if (
                                    not allow_file_access
                                    or not ALLOW_FILE_SCHEME
                                    or not file_root
                                    or not file_path
                                    or not _is_path_within_root(file_path, file_root)
                                ):
                                    blocked_urls.append(
                                        {
                                            "url": target,
                                            "reason": "redirect_target_not_allowed",
                                            "from": resp.url,
                                        }
                                    )
                            elif t_parsed.scheme:
                                blocked_urls.append(
                                    {
                                        "url": target,
                                        "reason": "redirect_target_not_allowed",
                                        "from": resp.url,
                                    }
                                )
                except Exception:
                    return

            page.on("response", _on_response)

            if media in {"screen", "print"}:
                page.emulate_media(media=media)

            if force_exact_colors:
                try:
                    page.add_style_tag(
                        content="html { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }",
                    )
                except Exception:
                    pass

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

            if wait_networkidle:
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

            if wait_window_status:
                try:
                    page.wait_for_function(
                        "window.status === status",
                        arg=wait_window_status,
                        timeout=10000,
                    )
                except Exception:
                    raise HTTPException(status_code=409, detail="Timeout esperando window.status")

            if wait_for_expression:
                try:
                    page.wait_for_function(wait_for_expression, timeout=10000)
                except Exception:
                    raise HTTPException(status_code=409, detail="Timeout esperando expresión")

            if wait_for_selector:
                try:
                    page.wait_for_selector(wait_for_selector, state="visible", timeout=10000)
                except Exception:
                    raise HTTPException(status_code=409, detail="Timeout esperando selector")

            if wait_delay_ms > 0:
                page.wait_for_timeout(wait_delay_ms)

            if strict_resources and resource_failures:
                raise HTTPException(
                    status_code=409,
                    detail=f"Falló la carga de recursos: {resource_failures[:5]}",
                )

            if strict_resources and resource_bad_status:
                raise HTTPException(
                    status_code=409,
                    detail=f"Recursos con HTTP status inválido: {resource_bad_status[:5]}",
                )

            blocked_nav = [b for b in blocked_urls if b.get("is_navigation") or "from" in b]
            if blocked_nav:
                raise HTTPException(
                    status_code=400,
                    detail=f"Navegación/redirect bloqueado: {blocked_nav[:5]}",
                )

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
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
    def _route_handler(route, request):
        url = request.url
        parsed = urlparse(url)

        def _block(reason: str):
            try:
                blocked_urls.append(
                    {
                        "url": url,
                        "reason": reason,
                        "is_navigation": bool(getattr(request, "is_navigation_request", lambda: False)()),
                        "resource_type": getattr(request, "resource_type", None),
                    }
                )
            except Exception:
                pass
            route.abort()

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
                _block("remote_url_not_allowed")
                return
            route.continue_()
            return

        if parsed.scheme == "file":
            if not allow_file_access or not ALLOW_FILE_SCHEME or not file_root:
                _block("file_scheme_not_allowed")
                return
            file_path = unquote(parsed.path or "")
            if not file_path:
                _block("file_invalid_path")
                return
            if not _is_path_within_root(file_path, file_root):
                _block("file_outside_root")
                return
            route.continue_()
            return

        if parsed.scheme in {"data", "blob", "about"}:
            route.continue_()
            return

        _block("scheme_not_allowed")

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
        _maybe_restart_persistent_browser()

        if CHROMIUM_PERSISTENT:
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
            browser = _get_chromium_browser()
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

            pdf = _render_with_browser(
                browser=browser,
                normalized_html=normalized_html,
                media=media,
                allow_remote=allow_remote,
                wait_delay_ms=wait_delay_ms,
                wait_for_selector=wait_for_selector,
                wait_for_expression=wait_for_expression,
                wait_window_status=wait_window_status,
                wait_networkidle=wait_networkidle,
                strict_resources=strict_resources,
                user_agent=user_agent,
                extra_http_headers=extra_http_headers,
                force_exact_colors=force_exact_colors,
                allow_file_access=allow_file_access,
                file_root=file_root,
                route_handler=_route_handler,
                t0=t0,
            )
        else:
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
                        "--no-zygote",
                        "--disable-pdf-tagging",
                        "--font-render-hinting=none",
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

                try:
                    pdf = _render_with_browser(
                        browser=browser,
                        normalized_html=normalized_html,
                        media=media,
                        allow_remote=allow_remote,
                        wait_delay_ms=wait_delay_ms,
                        wait_for_selector=wait_for_selector,
                        wait_for_expression=wait_for_expression,
                        wait_window_status=wait_window_status,
                        wait_networkidle=wait_networkidle,
                        strict_resources=strict_resources,
                        user_agent=user_agent,
                        extra_http_headers=extra_http_headers,
                        force_exact_colors=force_exact_colors,
                        allow_file_access=allow_file_access,
                        file_root=file_root,
                        route_handler=_route_handler,
                        t0=t0,
                    )
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass

        global _chromium_render_count
        _chromium_render_count += 1
        return pdf
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
    wait_delay_ms: int,
    wait_for_selector: str | None,
    wait_for_expression: str | None,
    wait_window_status: str | None,
    wait_networkidle: bool,
    strict_resources: bool,
    user_agent: str | None,
    extra_http_headers: dict[str, str] | None,
    force_exact_colors: bool,
    allow_file_access: bool,
    file_root: str | None,
) -> bytes:
    _validate_base_url(
        base_url,
        allow_remote=allow_remote,
        allow_file_access=allow_file_access,
        file_root=file_root,
    )
    extra_http_headers = _sanitize_extra_http_headers(extra_http_headers, allow_remote=allow_remote)

    acquired = _render_slots.acquire(blocking=False)
    if not acquired:
        raise HTTPException(status_code=503, detail="Cola de render saturada")

    with _render_inflight_lock:
        global _render_inflight
        _render_inflight += 1

    future = _executor.submit(
        _render_pdf_chromium,
        html=html,
        base_url=base_url,
        allow_remote=allow_remote,
        media=media,
        wait_delay_ms=wait_delay_ms,
        wait_for_selector=wait_for_selector,
        wait_for_expression=wait_for_expression,
        wait_window_status=wait_window_status,
        wait_networkidle=wait_networkidle,
        strict_resources=strict_resources,
        user_agent=user_agent,
        extra_http_headers=extra_http_headers,
        force_exact_colors=force_exact_colors,
        allow_file_access=allow_file_access,
        file_root=file_root,
    )

    def _release(_: Future[bytes]):
        with _render_inflight_lock:
            global _render_inflight
            _render_inflight = max(0, _render_inflight - 1)
        _render_slots.release()

    future.add_done_callback(_release)
    try:
        return future.result(timeout=RENDER_TIMEOUT_SECONDS + 15.0)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Timeout generando PDF") from exc


class PdfRequest(BaseModel):
    html: str = Field(..., description="HTML completo a renderizar")
    filename: str | None = Field("documento.pdf", description="Nombre sugerido del PDF")
    base_url: str | None = Field(
        None,
        description=(
            "Base URL para resolver rutas relativas. Soporta https:// (solo con token remoto + allowlist) y file:// "
            "solo en /pdf/upload cuando ALLOW_FILE_SCHEME=1 (restringido al directorio temporal del upload)."
        ),
    )
    media: str | None = Field(
        None,
        description=(
            "Media a emular: 'screen' (default) o 'print'. Si no se envía y el HTML define '@page { size: ... }', "
            "la API usa automáticamente 'print' para respetar el tamaño de hoja definido por CSS."
        ),
    )

    wait_delay_ms: int | None = Field(None, description="Espera fija (ms) antes de generar el PDF")
    wait_for_selector: str | None = Field(None, description="Selector CSS a esperar (visible) antes de generar el PDF")
    wait_for_expression: str | None = Field(None, description="Expresión JS (debe evaluar a true) a esperar antes de generar el PDF")
    wait_window_status: str | None = Field(None, description="Espera a que window.status sea este valor")
    wait_networkidle: bool | None = Field(None, description="Si true, espera adicional a 'networkidle' antes de generar el PDF")

    strict_resources: bool | None = Field(None, description="Si true, falla si algún recurso (IMG/CSS/JS) falla o devuelve HTTP >= 400")
    user_agent: str | None = Field(None, description="Sobrescribe el User-Agent")
    extra_http_headers: dict[str, str] | None = Field(
        None,
        description=(
            "Headers extra por request (diccionario). Por defecto solo permitido con token remoto y se filtran headers sensibles "
            "(ej. Authorization/Cookie). También aplica límites de cantidad/tamaño."
        ),
    )
    force_exact_colors: bool | None = Field(None, description="Si true, inyecta print-color-adjust: exact para colores más consistentes")


def _default_media() -> str:
    return "screen"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/deep")
def health_deep():
    if sync_playwright is None:
        raise HTTPException(status_code=500, detail="Chromium no disponible (Playwright no instalado)")

    timeout_seconds = max(15.0, HEALTH_DEEP_TIMEOUT_SECONDS)

    def _smoke() -> None:
        if CHROMIUM_PERSISTENT:
            browser = _get_chromium_browser()
            page = browser.new_page()
            try:
                page.set_content("<html><body>ok</body></html>", wait_until="domcontentloaded", timeout=5000)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
            return

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--no-zygote",
                    "--disable-pdf-tagging",
                    "--font-render-hinting=none",
                ],
            )
            try:
                page = browser.new_page()
                page.set_content("<html><body>ok</body></html>", wait_until="domcontentloaded", timeout=5000)
                page.close()
            finally:
                browser.close()

    started = time.time()
    future = _health_executor.submit(_smoke)
    try:
        future.result(timeout=timeout_seconds)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Timeout en health check") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Health check falló: {exc}") from exc

    elapsed_ms = int((time.time() - started) * 1000)
    _logger.info(json.dumps({"event": "health_deep", "status": "ok", "elapsed_ms": elapsed_ms}))

    return {"status": "ok"}


@app.get("/version")
def version():
    return {
        "service": "html-to-pdf-api",
        "playwright_available": sync_playwright is not None,
        "max_html_bytes": MAX_HTML_BYTES,
        "render_timeout_seconds": RENDER_TIMEOUT_SECONDS,
        "workers": WORKERS,
        "max_render_queue": MAX_RENDER_QUEUE,
        "render_inflight": _render_inflight,
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

    if payload.media is None and _html_defines_page_size(html):
        media = "print"
    else:
        media = (payload.media or _default_media()).strip().lower()
    if media not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    wait_delay_ms = int(payload.wait_delay_ms or 0)
    wait_for_selector = (payload.wait_for_selector or "").strip() or None
    wait_for_expression = (payload.wait_for_expression or "").strip() or None
    wait_window_status = (payload.wait_window_status or "").strip() or None
    wait_networkidle = bool(payload.wait_networkidle) if payload.wait_networkidle is not None else False
    strict_resources = bool(payload.strict_resources) if payload.strict_resources is not None else REMOTE_STRICT_RESOURCES
    user_agent = (payload.user_agent or "").strip() or None
    extra_http_headers = payload.extra_http_headers
    force_exact_colors = bool(payload.force_exact_colors) if payload.force_exact_colors is not None else FORCE_EXACT_COLORS

    try:
        started = time.time()
        pdf_bytes = _render_pdf_with_timeout(
            html=html,
            base_url=payload.base_url,
            allow_remote=auth.allow_remote,
            media=media,
            wait_delay_ms=wait_delay_ms,
            wait_for_selector=wait_for_selector,
            wait_for_expression=wait_for_expression,
            wait_window_status=wait_window_status,
            wait_networkidle=wait_networkidle,
            strict_resources=strict_resources,
            user_agent=user_agent,
            extra_http_headers=extra_http_headers,
            force_exact_colors=force_exact_colors,
            allow_file_access=False,
            file_root=None,
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
    wait_delay_ms: int = Query(0, description="Espera fija (ms) antes de generar el PDF"),
    wait_for_selector: str | None = Query(None, description="Selector CSS a esperar (visible)"),
    wait_for_expression: str | None = Query(None, description="Expresión JS (debe evaluar a true) a esperar"),
    wait_window_status: str | None = Query(None, description="Espera a que window.status sea este valor"),
    wait_networkidle: bool = Query(False, description="Si true, espera adicional a 'networkidle'"),
    strict_resources: bool | None = Query(None, description="Si true, falla si algún recurso falla o devuelve HTTP >= 400"),
    user_agent: str | None = Query(None, description="Sobrescribe el User-Agent"),
    extra_http_headers_json: str | None = Query(None, description="Headers extra como JSON string (ej. {\"X-Foo\":\"bar\"})"),
    force_exact_colors: bool | None = Query(None, description="Si true, inyecta print-color-adjust: exact"),
    auth: _AuthContext = Depends(_require_api_key),
):
    if not html or not html.strip():
        raise HTTPException(status_code=400, detail="El cuerpo HTML es requerido")

    normalized_html = _normalize_html_input(html)
    _enforce_size_limit(normalized_html)

    if media is None and _html_defines_page_size(normalized_html):
        media_normalized = "print"
    else:
        media_normalized = (media or _default_media()).strip().lower()
    if media_normalized not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    strict_resources_val = strict_resources if strict_resources is not None else REMOTE_STRICT_RESOURCES
    force_exact_colors_val = force_exact_colors if force_exact_colors is not None else FORCE_EXACT_COLORS

    extra_headers: dict[str, str] | None = None
    if extra_http_headers_json:
        try:
            parsed = json.loads(extra_http_headers_json)
            if isinstance(parsed, dict):
                extra_headers = {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            raise HTTPException(status_code=400, detail="extra_http_headers_json inválido")

    try:
        started = time.time()
        pdf_bytes = _render_pdf_with_timeout(
            html=normalized_html,
            base_url=base_url,
            allow_remote=auth.allow_remote,
            media=media_normalized,
            wait_delay_ms=wait_delay_ms,
            wait_for_selector=(wait_for_selector or "").strip() or None,
            wait_for_expression=(wait_for_expression or "").strip() or None,
            wait_window_status=(wait_window_status or "").strip() or None,
            wait_networkidle=wait_networkidle,
            strict_resources=strict_resources_val,
            user_agent=(user_agent or "").strip() or None,
            extra_http_headers=extra_headers,
            force_exact_colors=force_exact_colors_val,
            allow_file_access=False,
            file_root=None,
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

    if payload.media is None and _html_defines_page_size(html):
        media = "print"
    else:
        media = (payload.media or _default_media()).strip().lower()
    if media not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    wait_delay_ms = int(payload.wait_delay_ms or 0)
    wait_for_selector = (payload.wait_for_selector or "").strip() or None
    wait_for_expression = (payload.wait_for_expression or "").strip() or None
    wait_window_status = (payload.wait_window_status or "").strip() or None
    wait_networkidle = bool(payload.wait_networkidle) if payload.wait_networkidle is not None else False
    strict_resources = bool(payload.strict_resources) if payload.strict_resources is not None else REMOTE_STRICT_RESOURCES
    user_agent = (payload.user_agent or "").strip() or None
    extra_http_headers = payload.extra_http_headers
    force_exact_colors = bool(payload.force_exact_colors) if payload.force_exact_colors is not None else FORCE_EXACT_COLORS

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
            wait_delay_ms=wait_delay_ms,
            wait_for_selector=wait_for_selector,
            wait_for_expression=wait_for_expression,
            wait_window_status=wait_window_status,
            wait_networkidle=wait_networkidle,
            strict_resources=strict_resources,
            user_agent=user_agent,
            extra_http_headers=extra_http_headers,
            force_exact_colors=force_exact_colors,
            allow_file_access=False,
            file_root=None,
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
    strict_resources: bool | None = Query(None, description="Si true, falla si algún recurso falla o devuelve HTTP >= 400"),
    force_exact_colors: bool | None = Query(None, description="Si true, inyecta print-color-adjust: exact"),
    user_agent: str | None = Query(None, description="Sobrescribe el User-Agent"),
    auth: _AuthContext = Depends(_require_api_key),
):
    if not html or not html.strip():
        raise HTTPException(status_code=400, detail="El campo 'html' es requerido")

    normalized_html = _normalize_html_input(html)
    _enforce_size_limit(normalized_html)

    if media is None and _html_defines_page_size(normalized_html):
        media_normalized = "print"
    else:
        media_normalized = (media or _default_media()).strip().lower()
    if media_normalized not in {"print", "screen"}:
        raise HTTPException(status_code=400, detail="media inválido (usa 'print' o 'screen')")

    strict_resources_val = strict_resources if strict_resources is not None else REMOTE_STRICT_RESOURCES
    force_exact_colors_val = force_exact_colors if force_exact_colors is not None else FORCE_EXACT_COLORS

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
                wait_delay_ms=0,
                wait_for_selector=None,
                wait_for_expression=None,
                wait_window_status=None,
                wait_networkidle=False,
                strict_resources=strict_resources_val,
                user_agent=(user_agent or "").strip() or None,
                extra_http_headers=None,
                force_exact_colors=force_exact_colors_val,
                allow_file_access=True,
                file_root=str(base_path),
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
