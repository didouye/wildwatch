# V0.5 -- Sharing and management

Date: 2026-05-04

## Goal

Bring the day-to-day curation features on top of the V0.4 catalog: tags,
favorites, public share links, a stats page, and bulk delete from the
gallery. The instance stays single-user, so UI actions remain open while
the `/api/*` JSON surface keeps its API-key gate for scripted usage.

## Decisions

- **Tags via a many-to-many table.** `tags` and `photo_tags` link tables.
  Names are case-insensitive (`COLLATE NOCASE`) so "bird" and "Bird" share
  one row. Tag names are free-form (no presets); the UI provides
  autocomplete from the existing list.
- **Favorites = boolean column** (`photos.is_favorite`). YAGNI on a
  separate timestamp; we already have `received_at` if we need to sort by
  recently saved.
- **Public share = random token in DB.** New nullable column
  `photos.share_token` with `UNIQUE` index. NULL means "not shared". The UI
  exposes share + revoke. Share view strips private metadata (hostname,
  system probes).
- **Stats page uses Chart.js via CDN.** A few canvases populated from the
  existing `/api/stats` data plus per-hour and per-tag aggregates.
- **Bulk delete from the gallery** with a "selection mode" toggle
  (htmx-swapped checkboxes), wired to a single endpoint that also serves
  bulk favorite/tag actions.
- **No web auth yet.** Same posture as V0.4b. UI actions are open;
  destructive ones live behind explicit buttons. Web auth + HTTPS together
  in V1.0.

## Schema diff

```sql
ALTER TABLE photos ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0;
ALTER TABLE photos ADD COLUMN share_token TEXT UNIQUE;

CREATE INDEX idx_photos_is_favorite ON photos(is_favorite) WHERE is_favorite = 1;

CREATE TABLE tags (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT
);

CREATE TABLE photo_tags (
    photo_id INTEGER NOT NULL,
    tag_id   INTEGER NOT NULL,
    PRIMARY KEY (photo_id, tag_id),
    FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
);

CREATE INDEX idx_photo_tags_tag ON photo_tags(tag_id);
```

A `migrations.py` module ships a small CLI that walks `PRAGMA table_info`
and applies the missing columns/tables idempotently, so an operator on a
V0.4 database can upgrade in place without losing data.

## Endpoints

### UI (no auth)

```
POST   /photos/{id}/favorite          toggle is_favorite (htmx returns the star)
POST   /photos/{id}/tags              form: tag names (CSV), syncs many-to-many
POST   /photos/{id}/share             generate share_token (rotate if existing)
POST   /photos/{id}/unshare           clear share_token
POST   /photos/bulk                   form: action + ids[] (delete|favorite|unfavorite|tag)
GET    /share/{token}                 public photo view (no nav, sanitized metadata)
GET    /share/{token}/file            public original download
GET    /share/{token}/thumb/{size}    public thumbnail
GET    /tags                          tags admin page (rename, delete, photo counts)
GET    /stats                         stats page with Chart.js
GET    /gallery                       + new ?favorite=true and ?tag=<name> filters
```

### REST API (existing API-key gate)

```
GET    /api/photos                    + ?favorite=true and ?tag=<name>
PATCH  /api/photos/{id}               body: { is_favorite, tags: [string] }
POST   /api/photos/bulk-delete        body: { ids: [int] }
GET    /api/tags                      list tags + photo counts
DELETE /api/tags/{id}                 drop tag (cascades photo_tags rows)
```

## UI flows

**Gallery**
- Filter bar gains a Tag select (built from the tags list) and a Favorites
  toggle.
- Each thumbnail shows a star overlay on hover; clicking POSTs
  `/photos/{id}/favorite` and htmx swaps the star.
- A "Select" toggle button enters selection mode: thumbnails get
  checkboxes, an action bar appears with "Delete", "Add tag", "Toggle
  favorite", and "Cancel".

**Photo detail**
- Star button (htmx, swaps in place).
- Tag input with chips: free-form text, comma to commit, autocomplete from
  existing tags. Submit POSTs to `/photos/{id}/tags` and rerenders the chip
  list.
- Share button: POST `/photos/{id}/share` -> banner with the public URL
  and a Revoke button that POSTs `/photos/{id}/unshare`.

**Public share view**
- Renders only the photo, captured_at, motion score, tags, and a download
  link. Hostname, exposure, sensor, system probes are stripped.

**Stats page**
- Reuses `/api/stats` plus a few extra aggregates.
- Charts: photos per day (bar), photos per hour-of-day (bar), photos per
  hostname (donut), photos per tag (horizontal bar). Plus headline figures
  (total, last 7 days, top tag).

## Privacy contract for shared photos

The `share_view.html` template renders only:
- The 800 px thumbnail (no original by default; download requires explicit
  click)
- Captured timestamp (date + UTC time)
- Motion score
- Tags

It excludes hostname, sensor make/exposure, all system probes (CPU temp,
memory, load) and the file path. The download endpoint serves the original
JPEG but only via the share token, which is opaque and revocable.

## Tests (TDD)

Favorites: toggle on, toggle off, gallery filter `favorite=true`.
Tags: create new tag on first use, link existing tag, replace set on
PATCH, listing returns counts, gallery filter, deleting a tag unlinks
photos but does not delete them.
Share: POST creates a token, GET /share/{token} returns 200 and HTML
without hostname; POST /unshare invalidates; missing token returns 404.
Bulk: delete N photos removes DB rows, thumbnail caches, and source files;
unknown ids in the payload are silently ignored.
Stats: page renders, includes the canvas IDs and the raw data dict for
JS hydration.
Migrations: starting from a V0.4 schema, the script adds the new columns
and tables without losing rows.

## Out of scope (V1.0)

- Web authentication (login/password, sessions)
- HTTPS termination via reverse proxy
- Per-user permissions (multi-user mode)
- API rate limiting
