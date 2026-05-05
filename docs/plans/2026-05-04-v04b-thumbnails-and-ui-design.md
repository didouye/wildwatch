# V0.4b -- Thumbnails and web UI

Date: 2026-05-04

## Goal

Build a server-rendered web UI on top of the V0.4a catalog: a paginated
gallery, a per-photo detail page, and date/hostname filters. Generate three
thumbnail sizes (150 / 400 / 800 px) so the browser only loads what it
needs. Keep the operator surface friction-free for V0.4b: no login, no
account management. The /api/* endpoints remain protected by the existing
WILDWATCH_API_KEY (consumed by the RPi capture client).

This is the second half of V0.4. The catalog and REST API were delivered in
V0.4a. Multi-user web auth is deferred (probably V1.0 alongside HTTPS).

## Decisions

- **No web auth.** UI routes are open. The user's deployment is single-user
  on a private VPS for now.
- **Stack: Jinja2 + htmx + Tailwind via CDN.** Server-rendered HTML, htmx
  for partial updates (filter form, pagination). Tailwind via CDN avoids a
  build pipeline in V0.4b; the production V1.0 milestone will swap in a
  built CSS bundle.
- **Thumbnails: async-first with lazy fallback.** Pillow generates the 3
  sizes inside FastAPI BackgroundTasks at upload time. If a thumbnail is
  missing when requested (server crashed mid-generation, photo ingested via
  reindex, or hand-removed), the GET /thumb endpoint generates it on the
  fly before serving. Robust without Redis/Celery.
- **Storage layout for thumbnails:** a sibling directory `thumbnails/`
  next to `photos/`, mirroring the `YYYY/MM/DD` tree. Easy to purge,
  easy to exclude from backups since it is fully reconstructible.

## File layout

```
server/
|- src/wildwatch_server/
|  |- thumbnails.py             # Pillow generation + ensure_thumbnail
|  |- routes/
|  |  |- thumbnails.py          # GET /thumb/{size}/{id}
|  |  |- ui.py                  # /, /gallery, /photos/{id}, /photos/{id}/download
|  |  |- ...                    # existing routers
|  |- templates/
|  |  |- base.html
|  |  |- gallery.html
|  |  |- _gallery_grid.html
|  |  |- photo_detail.html
|- static/                      # optional custom CSS
|- tests/
|  |- test_thumbnails.py
|  |- test_ui.py
```

## Thumbnail module

```python
THUMBNAIL_SIZES = (150, 400, 800)

def thumbnail_dir() -> Path: ...
def thumbnail_path(file_path: str, size: int) -> Path: ...

def generate_one(file_path: str, size: int) -> Path:
    """Idempotent. Pillow opens the source, applies EXIF transpose, fits
    into (size, size) preserving aspect ratio, saves JPEG q=80 optimize=True."""

def generate_all(file_path: str) -> list[Path]:
    """Generate all THUMBNAIL_SIZES. Used by BackgroundTasks at upload."""

def ensure_thumbnail(file_path: str, size: int) -> Path:
    """Lazy fallback. If missing, generates synchronously, then returns the path."""
```

EXIF transpose matters because the Camera Module 3 ships portrait images
with rotation flags. Quality 80 keeps the 800 px JPEG well under 100 KB
without visible artifacts at gallery viewing distances.

## Routes

```
GET  /                          redirect to /gallery
GET  /gallery                   gallery page (full or htmx partial)
       ?limit&offset&from&to&hostname
GET  /photos/{id}               detail page
GET  /photos/{id}/download      original JPEG, no auth (UI alias)
GET  /thumb/{size}/{id}         cached thumbnail with lazy fallback
                                Cache-Control: public, max-age=86400
POST /api/admin/regen_thumbnails  rebuild missing thumbnails (auth)
```

The `/gallery` handler inspects the `HX-Request` header. When set, it
returns just the `_gallery_grid.html` partial. Otherwise it renders the
full page. This keeps URL-based pagination/filtering (deep linking) while
also delivering instant updates via htmx.

## Upload flow change

`POST /api/photos` (modified):
1. Validate auth + content-type (existing)
2. Write file to disk (existing)
3. INSERT into DB (V0.4a)
4. **NEW**: `background_tasks.add_task(generate_all, photo.file_path)`
5. Return response (client does not wait for thumbnails)

Idempotency: `generate_one` is a no-op if the target already exists. Calling
`generate_all` after a reindex that triggers thumbnails is safe.

## UI mocks

- Gallery: header (title + photo count), filter form (date range, hostname
  select populated from DISTINCT), thumbnail grid
  (`grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6`), each cell
  is `<a><img src="/thumb/150/{id}"></a>` with hover overlay
  (date + motion_score). Pagination (Prev / Page X of Y / Next) wired via
  htmx `hx-get` targeting `#gallery-grid`.
- Detail: 800 px thumbnail, metadata panel (captured_at, received_at,
  hostname, motion_score, frame_index/burst_size, camera resolution,
  sensor model + exposure + gain + lux, cpu_temp + memory + load),
  download button -> `/photos/{id}/download`, prev/next links by
  captured_at order.

## Tests (TDD)

Thumbnails:
- `test_generate_one_creates_jpeg`
- `test_generate_one_idempotent`
- `test_generate_all_creates_three_files`
- `test_ensure_thumbnail_lazy_creates_when_missing`
- `test_thumb_endpoint_returns_image`
- `test_thumb_invalid_size_returns_400`
- `test_thumb_unknown_id_404`
- `test_thumb_missing_source_404`
- `test_upload_schedules_background_thumbnail`

UI:
- `test_gallery_full_page_renders`
- `test_gallery_htmx_returns_partial`
- `test_gallery_filters_by_date_and_hostname`
- `test_photo_detail_page`
- `test_photo_detail_404`
- `test_photo_download_serves_original_without_auth`

Admin:
- `test_regen_thumbnails_creates_missing`

## Out of scope

- Web authentication (login/password, sessions) -- V1.0 goal.
- HTTPS termination -- handled by a reverse proxy in V1.0.
- Tag editing, favorites, share links -- V0.5.
- Bulk delete, multi-select -- V0.5.
