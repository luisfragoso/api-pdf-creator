# API PDF Creator

Proyecto mínimo para generar PDFs en Node.js con 3 enfoques:

- `jsPDF`: PDF básico (texto simple) generando un `Buffer`.
- `PDFKit`: PDF programático con control fino (streams/buffers).
- `Puppeteer`: HTML -> PDF (ideal para reportes con CSS).

## Requisitos

- Node.js 18+ recomendado

## Instalación

```bash
npm install
```

## Generar PDFs (ejemplos)

### 1) jsPDF

```bash
npm run generate:jspdf
```

Salida:

- `examples/out-jspdf.pdf`

### 2) PDFKit

```bash
npm run generate:pdfkit
```

Salida:

- `examples/out-pdfkit.pdf`

### 3) HTML -> PDF (Puppeteer)

```bash
npm run generate:html
```

Salida:

- `examples/out-html.pdf`

La plantilla está en:

- `examples/template.html`

## ¿Cuál usar?

- Si quieres **algo rápido y simple**: `jsPDF`.
- Si quieres **control total** (paginación, tablas manuales, etc.): `PDFKit`.
- Si ya tienes un diseño en **HTML/CSS** o un “reporte bonito”: `Puppeteer`.

---

# HTML to PDF API (Python/FastAPI + WeasyPrint)

Este repo también incluye una API en Python para convertir HTML → PDF.

## Levantar con Docker

1) Crea tu archivo `.env` (no lo subas a git):

   - Copia `.env.example` → `.env`
   - Cambia los tokens y hosts a valores reales

2) Levanta el servicio:

### Local (solo API, recomendado para desarrollo)

```bash
docker compose up --build
```

Esto levanta la API en:

- `http://localhost:8000/health`
- `http://localhost:8000/docs`

## Deploy en producción (subdominio + HTTPS)

Este repo incluye un reverse proxy **Caddy** que genera certificados TLS automáticamente (Let's Encrypt).

### Requisitos

- Tu subdominio (ej. `pdf.tudominio.com`) debe apuntar a la IP del servidor (registro DNS tipo **A**).
- En el servidor, abre puertos **80** y **443**.

### Pasos

1) En el servidor, copia `.env.example` → `.env`.
2) Edita `.env` y configura:

- `DOMAIN=pdf.tudominio.com`
- `ACME_EMAIL=tu-correo@tudominio.com`
- Tokens: `PDF_API_KEYS` / `PDF_REMOTE_API_KEYS`
- `ALLOWED_REMOTE_HOSTS` (si usarás recursos remotos)

3) Levanta (modo producción con Caddy):

```bash
docker compose --profile prod up -d --build
```

4) Prueba:

- Swagger: `https://pdf.tudominio.com/docs`
- Health: `https://pdf.tudominio.com/health`

## Swagger / OpenAPI

- Swagger UI: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

## Observabilidad

- La API devuelve `X-Request-Id` en todas las respuestas.
- Puedes enviar tu propio `X-Request-Id` en el request.
- Endpoint de versión/config: `GET /version`

## Límites y timeouts

- Tamaño máximo de HTML (app): `MAX_HTML_BYTES`
- Timeout de render (app): `RENDER_TIMEOUT_SECONDS`
- Rate limit por token (req/min):
  - `RATE_LIMIT_NORMAL_PER_MIN`
  - `RATE_LIMIT_REMOTE_PER_MIN`

## Autenticación (tokens)

Los devs NO deben hardcodear tokens en el código. Se configuran por ambiente en `.env`:

- `PDF_API_KEYS`: tokens normales (no permiten recursos remotos)
- `PDF_REMOTE_API_KEYS`: tokens con permiso a recursos remotos (solo HTTPS + allowlist)

En requests puedes autenticar con:

- Header `Authorization: Bearer <token>`
- O header `X-API-Key: <token>`

## Endpoints

### `POST /pdf`

Recibe JSON:

```json
{ "html": "<html>...</html>", "filename": "reporte.pdf", "base_url": null }
```

### `POST /pdf/raw`

Recibe HTML crudo (`text/html`) y responde PDF.

### `POST /pdf/upload`

Recibe `multipart/form-data`:

- Campo `html` (string)
- Campo(s) `files` (imágenes, etc.)

En el HTML usa rutas relativas, por ejemplo: `<img src="logo.png">`.

### `POST /jobs`

Crea un job asíncrono para generar el PDF (útil para n8n si no quieres esperar el render en una sola llamada).

Request (JSON):

```json
{ "html": "<html>...</html>", "filename": "reporte.pdf", "base_url": null }
```

Response:

```json
{ "job_id": "...", "status": "queued" }
```

### `GET /jobs/{job_id}`

Devuelve estado:

- `queued`
- `running`
- `done`
- `error`

### `GET /jobs/{job_id}/pdf`

Descarga el PDF cuando el job está en `done`.

## Ejemplo (PowerShell)

```powershell
$html = Get-Content -Raw -Encoding UTF8 ".\Reporte Natura.html"

Invoke-WebRequest `
  -Uri "http://localhost:8000/pdf/raw?filename=Reporte%20Natura.pdf" `
  -Headers @{ Authorization = "Bearer client-token-2" } `
  -Method Post `
  -ContentType "text/html; charset=utf-8" `
  -Body $html `
  -OutFile "Reporte Natura.pdf"
```

## Uso desde n8n (backend-only)

En un nodo HTTP Request:

- Method: `POST`
- URL: `https://tu-subdominio.com/pdf/raw?filename=reporte.pdf`
- Headers:
  - `Authorization: Bearer <TU_TOKEN>`
  - `Content-Type: text/html; charset=utf-8`
- Body: el HTML

### Flujo recomendado (n8n) con jobs

1) `POST /jobs` (JSON) para crear el job.
2) Poll con `GET /jobs/{job_id}` hasta que `status=done`.
3) Descargar con `GET /jobs/{job_id}/pdf`.

## Seguridad (remotos)

Los recursos remotos (CSS/imagenes por URL) están controlados por:

- Token con permiso (`PDF_REMOTE_API_KEYS`)
- Solo HTTPS
- Allowlist de hosts: `ALLOWED_REMOTE_HOSTS`
