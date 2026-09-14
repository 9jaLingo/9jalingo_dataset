# Dataset review console — finalized design

Continues the design worked out in chat (`pidgin_review_console.html`,
single-language) into a multi-language tool. Three pieces:

- [`review_console.html`](review_console.html) — the browser UI. Static,
  no build step. Language-agnostic: it reads [`languages.json`](languages.json)
  for known languages and also accepts `?lang=`, or a fully custom
  `?dataset=org/repo&config=default&split=train&text=text` for anything not
  in that file yet.
- [`languages.json`](languages.json) — the one file to edit to add a
  language, fix a dataset repo id, or mark one `private`. No code change,
  no redeploy needed if it's served next to the HTML.
- [`review_server/`](review_server/) — the companion backend that writes
  parquet and pushes to Hugging Face, and also proxies the read paths that
  need authentication (private/gated source datasets). See its own README
  for run/deploy details. **It also serves the console itself** —
  `<backend url>/review_console.html?lang=<key>` works with nothing else
  hosted.

## What changed from the chat's last plan, and why

**Each language gets its own repo**, not a folder in one shared repo. Set
`TARGET_REPO_TEMPLATE` (e.g. `voicedata/9jalingo-reviewed-{language}`) once
as a backend env var and every language's shards land in its own
`voicedata/9jalingo-reviewed-pidgin`, `...-igbo`, etc. — computed
automatically from the language key, nothing to configure per language.
(The chat's original idea was a branch per language on one repo; a repo
per language is simpler still and avoids that branch inheriting the
source repo's full history.)

**Page size defaults to 100, selectable up to 1000** (100/200/500/1000, in
the toolbar) — this was originally a hardcoded constant on the theory that
a shard's filename (`train-000000-000099.parquet`) encoding its row range
made a variable size risky. It isn't, in practice: `committed_ranges` was
already tracked as arbitrary merge-able intervals (see "Duplicate
protection" in `review_server/README.md`), not fixed-size slots, so
changing page size — even mid-review — doesn't misalign anything.

The real constraint turned out to be elsewhere: Hugging Face's
`datasets-server` `/rows` API caps a single call's `length` at 100 no
matter what's requested. A page size above 100 is built by chunking into
multiple ≤100-row calls and stitching the results together — done in the
browser (`chunkRanges`/`mergeRowChunks` in `review_console.html`) for
browsing, and *separately* in the backend (`_fetch_rows_json` in
`main.py`) for both its own `/api/rows` proxy and — critically —
`commit_page`, since that's what actually fetches and writes shard audio.
Missing the backend side of this would have been silent data loss: a
500-row commit would fetch/write only the first 100 rows while still
recording the full 500 as committed in `progress.json`.

**One flexible console, not one file per language.** It's one HTML file,
parameterized by `languages.json` + URL params. Share
`review_console.html?lang=hausa`, `?lang=igbo`, etc. — same file, same
deploy, different starting language, and each person's local edit state is
kept separate (namespaced by language in `localStorage`) even in the same
browser.

**Private/gated source datasets route through the backend.** A browser
calling `datasets-server` directly for a private dataset gets a 401 — it
has no credentials to send. Mark a language `"private": true` in
`languages.json` and the console instead calls the backend's `/api/rows`
and `/api/asset` proxies, which carry the backend's own `HF_TOKEN`
(needs read+write — write for pushing shards, read for this). The token
never reaches the browser either way.

**`localStorage` instead of `window.storage`.** The original file called a
`window.storage.get/set` API that isn't available on a plain static
deploy. The console now uses real browser `localStorage` — works
identically everywhere. (One-time best-effort: if `window.storage` happens
to still be reachable and this is the `pidgin` language with no local data
yet, it tries importing old edits once, so nothing already captured is
lost.)

## Deploying

**Simplest path — one Render service does everything:** deploy
`review_server/` per its README; it serves the console itself at
`/review_console.html`. Share `https://<service>.onrender.com/review_console.html?lang=<key>`
per reviewer. The console auto-detects it's same-origin with its backend
and fills in the Sync panel's server URL for you.

**Or split them:** host `review_console.html` + `languages.json`
separately (Vercel, GitHub Pages, any static host) and point its Sync
panel at a `review_server/` deployed on Render. Useful if you want the
console at a friendlier/fixed URL independent of the backend. Not Vercel
for the backend itself — serverless timeout (10s free tier) is too short
for a 100-row audio shard upload.

## Adding a new language

1. Confirm the HF dataset repo id for that language, and whether it's
   private/gated (only `voicedata/final_pidgin` is verified public so far;
   `voicedata/final_igbo` is confirmed private; `final_hausa`/`final_yoruba`
   in `languages.json` are placeholders marked `_TODO`).
2. Add/edit its entry in `languages.json` (set `"private": true` if it's
   private/gated).
3. Share `review_console.html?lang=<key>` with whoever's reviewing it. Its
   own repo under `TARGET_REPO_TEMPLATE` is created automatically on first commit.

## What's still manual / not yet done

- **Before this backend's URL is ever shared beyond the internal team:**
  re-add access control. As of 2026-09-11 it's deliberately unauthenticated
  (`REVIEW_TOKEN` unset, and `review_console.html` has no field to send one
  — both removed intentionally for frictionless internal use) — anyone with
  the URL can push shards or read private datasets through it. Fixing this
  means setting `REVIEW_TOKEN` again on the backend AND restoring a way for
  the console to send it back (it used to have a token input in the Sync
  panel). See the `REVIEW_TOKEN` row in `review_server/README.md`.
- The `hausa`/`yoruba` repo ids in `languages.json` are placeholders —
  verify the real HF dataset repo id (and private/gated status) for each
  before pointing reviewers at them.
- Nothing here has been run end-to-end against real infrastructure by me —
  this is code, not a verified working deploy. Test `review_server`
  locally against a throwaway `TARGET_REPO_TEMPLATE` before pointing it at
  anything real.
