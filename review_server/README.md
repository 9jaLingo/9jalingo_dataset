# review_server

Companion backend for [`review_console.html`](../review_console.html). The
browser console is mostly read-only against Hugging Face's public preview
APIs and can't write parquet — this service does the parts that need a real
Hugging Face token: writing/pushing shards, and reading private/gated
source datasets the browser can't reach on its own.

It also **serves the console itself** — once deployed, `<this service's
URL>/review_console.html?lang=<key>` is a complete reviewer link with
nothing else to host.

## Run locally

```bash
cd 9jalingo_dataset/review_server
pip install -r requirements.txt
export HF_TOKEN=hf_xxx                                    # read+write on the Hub
export REVIEW_TOKEN=some-secret                           # console must send this back in X-Review-Token
export TARGET_REPO_TEMPLATE="voicedata/9jalingo-reviewed-{language}"
uvicorn main:app --port 8787
```

Open `http://127.0.0.1:8787/review_console.html?lang=pidgin`. In the Sync
panel, the server URL defaults to same-origin automatically when the console
is loaded from this backend's own `/review_console.html` route — nothing to
paste in. Set the token to the same `REVIEW_TOKEN` value and hit "save".

## Deploy (Render, not Vercel)

`render.yaml` in this folder is a ready-made Render blueprint (New ->
Blueprint in the Render dashboard, point it at this repo). Set `HF_TOKEN`
and `REVIEW_TOKEN` in the dashboard's Environment tab after creating it —
they're deliberately left out of `render.yaml`.

**Why not Vercel:** Vercel serverless functions have hard execution timeouts
(10s free tier, 60s Pro). A 100-row shard with audio can take longer than
that to fetch and push. Render web services just run the process, no
timeout. You *could* still host a copy of `review_console.html` statically
on Vercel/GitHub Pages if you want it at a separate URL — it only talks to
this backend over HTTP — but there's no need to; this service serves it too.

## Environment variables

| var | required | meaning |
|---|---|---|
| `PYTHON_VERSION` | **required on Render** | set to `3.11.9`. Render's own default has drifted to a Python version too new to have prebuilt wheels yet for `pydantic-core` (a Rust extension); without one, pip tries to compile it from source and fails outright in Render's build sandbox. Not needed running locally with your own already-installed Python. |
| `HF_TOKEN` | yes | needs **read + write** — write to push shards, read to fetch rows/audio from private or gated source datasets (e.g. Igbo) |
| `REVIEW_TOKEN` | strongly recommended | shared secret the console sends as `X-Review-Token` (or `?token=` for the `<audio>` proxy, which can't set headers); unset = no auth, anyone with the URL can push shards or read your private datasets through it |
| `TARGET_REPO_TEMPLATE` | no (defaults to `voicedata/9jalingo-reviewed-{language}`) | **set once** — every language automatically gets its own repo by substituting its key in for `{language}` (e.g. `voicedata/9jalingo-reviewed-pidgin`, `...-igbo`). Omit `{language}` entirely if you'd rather force everything into one shared repo. |
| `TARGET_REPO_PRIVATE` | no (defaults `false`, i.e. public) | enforced on every `/api/progress`/`/api/commit-page` call, not just at creation — flips an already-existing repo's visibility to match if it drifted (e.g. from an earlier default) |
| `ALLOWED_ORIGINS` | no (defaults `*`) | comma-separated CORS origins; only matters if you host the console at a *different* origin than this backend |

## Endpoints

- `GET /review_console.html`, `GET /languages.json`, `GET /` — serves the console
- `GET /api/config` — `{"target_repo_template": ...}`, unauthenticated (not secret)
- `GET /api/rows?dataset=&config=&split=&offset=&length=` — authenticated proxy for datasets-server's row preview, for private/gated source datasets
- `GET /api/asset?url=<hf resolve url>` — authenticated proxy for one audio file, for the same reason (a hard `huggingface.co`/`hf.co` host allowlist keeps this from being an open proxy)
- `GET /api/progress?language=` — committed page ranges + the resolved `target_repo` for that language
- `POST /api/commit-page` — writes and pushes one shard

## What it writes

Each language's **own** repo (from `TARGET_REPO_TEMPLATE`):

```
voicedata/9jalingo-reviewed-pidgin/
  data/train-000000-000099.parquet
  data/train-000100-000199.parquet
  progress.json          # {"language": "pidgin", "committed_ranges": [[0,100],[100,200]]}

voicedata/9jalingo-reviewed-igbo/
  data/train-000000-000099.parquet
  progress.json
```

## Duplicate protection

`progress.json`'s `committed_ranges` is the source of truth for which row
indices already have audio sitting in a shard on the Hub. Every commit is
checked against it before anything is written:

- **Exact re-commit** of a page that's already fully committed (same
  offset, same size) is treated as a deliberate correction pass — it
  overwrites that one shard file whole.
- **Partial overlap** — a page that only partly intersects prior commits
  (can happen if the page size ever changes between sessions) — silently
  *drops just the already-committed rows* from what gets built and
  uploaded, the same way a manifest delete does, rather than rejecting the
  whole page. The response's `rows_skipped_duplicate` says how many. If
  every row in the page turns out to already be covered, nothing gets
  uploaded at all — no empty/duplicate shard file.

`committed_ranges` itself is stored merged (overlapping or adjacent
intervals collapse into one), so it stays a compact, accurate record of
exactly which row indices have already been written, no matter how the
pages committing them lined up.

## Private / gated source datasets

Mark a language `"private": true` in `languages.json` and the console
routes both row-listing and audio playback for it through `/api/rows` and
`/api/asset` on this backend instead of calling Hugging Face directly from
the browser — using `HF_TOKEN`'s own read access, the same account that
already has read+write to that dataset. Requires a server URL to be
configured in the console; browsing a `private` language with no backend
set will fail fast with a clear message rather than a raw 401.

## Concurrency

Multiple people reviewing different languages (or the same language) at
once is expected — each commit is serialized server-side with a lock so two
simultaneous commits can't corrupt a language's `progress.json`, and the
overlap check above stops two people from silently clobbering each other's
half-finished page.
