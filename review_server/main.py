"""
review_server/main.py — companion backend for review_console.html

The browser console can't write parquet or hold a Hugging Face write token,
so this small FastAPI service does the actual "commit this page" work, and
also proxies a few read paths that need authentication (private/gated
datasets) without ever putting that token in the browser:

  - GET  /review_console.html, /languages.json, /  — serves the console
    itself, so a Render deploy of just this backend is a complete, working
    reviewer link with nothing else to host.
  - GET  /api/rows    — authenticated proxy for datasets-server's row
    preview API. Needed for private/gated source datasets (e.g. Igbo): the
    browser's own unauthenticated call to datasets-server 401s on those;
    this backend already holds a token with read access, so it can do the
    call and hand back the JSON.
  - GET  /api/asset    — authenticated proxy for one audio file. Necessary
    because <audio src> can't send an Authorization header itself, so for
    private/gated languages the audio element points at this instead of
    directly at Hugging Face.
  - GET  /api/progress, POST /api/commit-page — the actual review workflow:
    pull a page fresh via the same /rows mechanism above (a real range
    read — NOT `datasets.load_dataset`, which downloads the entire split
    before slicing and would try to pull this whole ~25GB dataset per
    page), apply edits/deletes, download just that page's audio, write one
    small parquet shard, push it.

Where shards land: every language gets its OWN repo (not a folder in one
shared repo) — see TARGET_REPO_TEMPLATE below. Set once as a Render env
var; every language's console tab computes its own target automatically.

    data/train-{offset:06d}-{offset+n-1:06d}.parquet
    progress.json      # committed row ranges for this language's repo, so
                        # a reviewer resuming later (or someone else
                        # reviewing the same language) can see what's done

Run locally:
    pip install -r requirements.txt
    export HF_TOKEN=hf_xxx                        # needs read+write; read
                                                    # covers private/gated
                                                    # source datasets too
    export REVIEW_TOKEN=some-secret                # shared secret the console sends back
    export TARGET_REPO_TEMPLATE="voicedata/9jalingo-reviewed-{language}"
    uvicorn main:app --port 8787
Then open http://127.0.0.1:8787/review_console.html — no separate static
host needed.

Deploy: see render.yaml — deploy this on Render (or any host that runs a
persistent process), NOT Vercel. Vercel's serverless functions time out
(10s on the free tier) well before a 100-row audio shard finishes
uploading; Render's web services have no such limit.
"""

import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.utils import EntryNotFoundError
from pydantic import BaseModel

HF_TOKEN = os.environ.get("HF_TOKEN")
REVIEW_TOKEN = os.environ.get("REVIEW_TOKEN")  # None disables auth -- fine for purely local use, not for a public deploy
TARGET_REPO_TEMPLATE = os.environ.get("TARGET_REPO_TEMPLATE", "voicedata/9jalingo-reviewed-{language}")
TARGET_REPO_PRIVATE = os.environ.get("TARGET_REPO_PRIVATE", "false").lower() == "true"
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]

HF_ROWS_URL = "https://datasets-server.huggingface.co/rows"
HF_INFO_URL = "https://datasets-server.huggingface.co/info"
_HF_ASSET_HOSTS = ("huggingface.co", "hf.co")

# review_console.html + languages.json live one directory up from this file
# (9jalingo_dataset/), regardless of what a host sets as its working dir.
_CONSOLE_DIR = Path(__file__).resolve().parent.parent

if not HF_TOKEN:
    raise RuntimeError("HF_TOKEN env var is required (needs read+write scope on the Hub)")
if not REVIEW_TOKEN:
    print("WARNING: REVIEW_TOKEN is not set -- anyone who finds this server's URL can push shards "
          "and read your private datasets through it. Fine for localhost-only use; set REVIEW_TOKEN "
          "before deploying publicly.")

api = HfApi(token=HF_TOKEN)
app = FastAPI(title="9jaLingo review-console backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serializes commits so two people finishing pages for the same language at
# the same moment can't race on that language's progress.json.
_lock = threading.Lock()
_repo_ensured = set()          # repo_ids already confirmed to exist
_features_cache = {}           # (dataset, config) -> datasets.Features


def _check_token(x_review_token: Optional[str], query_token: Optional[str] = None):
    if not REVIEW_TOKEN:
        return
    if x_review_token == REVIEW_TOKEN or query_token == REVIEW_TOKEN:
        return
    raise HTTPException(status_code=401, detail="missing/incorrect review token")


def _target_repo_for(language: str) -> str:
    try:
        return TARGET_REPO_TEMPLATE.format(language=language)
    except (KeyError, IndexError) as e:
        raise HTTPException(
            status_code=500,
            detail=f"TARGET_REPO_TEMPLATE ({TARGET_REPO_TEMPLATE!r}) is misconfigured: {e}",
        )


def _merge_ranges(ranges):
    """Collapse a list of [lo, hi) pairs into the minimal sorted, non-overlapping
    set -- so committed_ranges stays a clean record even after a partial-overlap
    commit adds a range that abuts or overlaps an existing one."""
    merged = []
    for lo, hi in sorted(r for r in ranges):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def _covered_row_indices(ranges, lo, hi):
    """Which row_idx in [lo, hi) are already inside some existing committed
    range -- used to dedupe a page against work already pushed to the Hub,
    instead of just rejecting the whole page on any overlap."""
    covered = set()
    for r_lo, r_hi in ranges:
        if r_hi <= lo or r_lo >= hi:
            continue
        covered.update(range(max(lo, r_lo), min(hi, r_hi)))
    return covered


def _ensure_repo(repo_id: str):
    if repo_id in _repo_ensured:
        return
    try:
        info = api.repo_info(repo_id, repo_type="dataset")
        # Repo already exists (e.g. created back when TARGET_REPO_PRIVATE
        # still defaulted to true) -- bring its visibility in line with the
        # current setting rather than leaving whatever it was created with.
        if bool(info.private) != TARGET_REPO_PRIVATE:
            api.update_repo_visibility(repo_id, private=TARGET_REPO_PRIVATE, repo_type="dataset")
    except Exception:
        api.create_repo(repo_id, repo_type="dataset", private=TARGET_REPO_PRIVATE, exist_ok=True)
    _repo_ensured.add(repo_id)


def _sync_repo_visibility_if_exists(repo_id: str):
    """Same visibility fix as _ensure_repo, but never creates the repo --
    just viewing progress for a language nobody has reviewed yet shouldn't
    eagerly create its (empty) repo. commit-page uses _ensure_repo instead,
    where creating on first commit is exactly what's wanted."""
    if repo_id in _repo_ensured:
        return
    try:
        info = api.repo_info(repo_id, repo_type="dataset")
    except Exception:
        return  # doesn't exist yet -- nothing to fix
    if bool(info.private) != TARGET_REPO_PRIVATE:
        api.update_repo_visibility(repo_id, private=TARGET_REPO_PRIVATE, repo_type="dataset")
    _repo_ensured.add(repo_id)


def _read_progress(repo_id: str, language: str) -> dict:
    try:
        path = hf_hub_download(repo_id, "progress.json", repo_type="dataset", token=HF_TOKEN)
        with open(path) as f:
            data = json.load(f)
            data.setdefault("committed_ranges", [])
            return data
    except EntryNotFoundError:
        return {"language": language, "committed_ranges": []}
    except Exception:
        # repo doesn't exist yet, or file missing for some other reason -- treat as "nothing committed"
        return {"language": language, "committed_ranges": []}


def _write_progress(repo_id: str, language: str, progress: dict):
    data = json.dumps(progress, indent=2).encode()
    api.upload_file(
        path_or_fileobj=io.BytesIO(data),
        path_in_repo="progress.json",
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=f"progress: {len(progress['committed_ranges'])} ranges committed",
    )


# ---------- cheap, authenticated, random-access row/asset fetching ----------
# (no full-dataset download -- see module docstring for why that matters)

def _fetch_rows_json(dataset: str, config: str, split: str, offset: int, length: int) -> dict:
    """Same API the console itself calls to browse -- but WITH auth, so
    private/gated source datasets work here even though they can't from an
    unauthenticated browser request."""
    try:
        resp = requests.get(
            HF_ROWS_URL,
            params={"dataset": dataset, "config": config, "split": split, "offset": offset, "length": length},
            headers={"Authorization": f"Bearer {HF_TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"datasets-server /rows failed: {e}")
    return resp.json()


def _fetch_features(dataset: str, config: str):
    """Dataset's column schema (incl. the Audio feature type) via the cheap
    /info endpoint -- a small JSON document, not a data download. Cached in
    memory since it doesn't change page to page."""
    key = (dataset, config)
    if key in _features_cache:
        return _features_cache[key]
    from datasets import Features
    try:
        resp = requests.get(
            HF_INFO_URL,
            params={"dataset": dataset, "config": config},
            headers={"Authorization": f"Bearer {HF_TOKEN}"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"datasets-server /info failed: {e}")
    info = resp.json().get("dataset_info")
    if not info or "features" not in info:
        raise HTTPException(status_code=502, detail="datasets-server /info returned no feature schema")
    features = Features.from_dict(info["features"])
    _features_cache[key] = features
    return features


def _audio_url(value):
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0].get("src")
    if isinstance(value, dict):
        return value.get("src")
    return None


def _download_bytes(url: str, timeout: int = 60) -> bytes:
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {HF_TOKEN}"}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"failed to download audio from {url}: {e}")
    return resp.content


def _is_hf_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in _HF_ASSET_HOSTS or any(host.endswith("." + h) for h in _HF_ASSET_HOSTS)


class CommitPageRequest(BaseModel):
    language: str
    dataset: str
    config: str = "default"
    split: str = "train"
    text_field: str = "text"
    audio_field: str = "audio"
    offset: int
    page_size: int
    edits: dict = {}      # {"<row_idx>": "new text"}
    deletes: list = []    # [row_idx, ...]


# ---------- static console ----------

@app.get("/")
def root():
    return RedirectResponse(url="/review_console.html")


@app.get("/review_console.html")
def serve_console():
    path = _CONSOLE_DIR / "review_console.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="review_console.html not found next to review_server/")
    return FileResponse(path, media_type="text/html")


@app.get("/languages.json")
def serve_languages_json():
    path = _CONSOLE_DIR / "languages.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="languages.json not found next to review_server/")
    return FileResponse(path, media_type="application/json")


# ---------- api ----------

@app.get("/health")
def health():
    return {"ok": True, "target_repo_template": TARGET_REPO_TEMPLATE}


@app.get("/api/config")
def get_config():
    # Not secret -- just the naming pattern, so the console can show an
    # accurate "shards go to..." label immediately, before it has asked
    # about any specific language yet.
    return {"target_repo_template": TARGET_REPO_TEMPLATE}


@app.get("/api/rows")
def get_rows(
    dataset: str, config: str = "default", split: str = "train", offset: int = 0, length: int = 100,
    x_review_token: Optional[str] = Header(default=None),
):
    """Authenticated stand-in for datasets-server's own /rows, for
    private/gated source datasets the browser can't call directly."""
    _check_token(x_review_token)
    return _fetch_rows_json(dataset, config, split, offset, length)


@app.get("/api/asset")
def get_asset(
    url: str,
    x_review_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,  # <audio src="..."> can't set headers, so also accept it as a query param
):
    """Authenticated audio fetch for a single row, for private/gated
    languages -- <audio src> is pointed at this instead of straight at
    Hugging Face when review_console.html marks a language `private`."""
    _check_token(x_review_token, query_token=token)
    if not _is_hf_url(url):
        raise HTTPException(status_code=400, detail="url must point at huggingface.co / hf.co")
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {HF_TOKEN}"}, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"failed to fetch asset: {e}")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/octet-stream"))


@app.get("/api/progress")
def get_progress(language: str, x_review_token: Optional[str] = Header(default=None)):
    _check_token(x_review_token)
    repo_id = _target_repo_for(language)
    _sync_repo_visibility_if_exists(repo_id)
    data = _read_progress(repo_id, language)
    data["target_repo"] = repo_id
    return data


@app.post("/api/commit-page")
def commit_page(req: CommitPageRequest, x_review_token: Optional[str] = Header(default=None)):
    _check_token(x_review_token)
    if req.page_size <= 0:
        raise HTTPException(status_code=400, detail="page_size must be positive")

    repo_id = _target_repo_for(req.language)

    with _lock:
        _ensure_repo(repo_id)
        progress = _read_progress(repo_id, req.language)
        lo, hi = req.offset, req.offset + req.page_size

        exact_match = any(r[0] == lo and r[1] == hi for r in progress["committed_ranges"])
        # A deliberate re-commit of a page that's already fully committed
        # (a correction pass) should rewrite it whole, not dedupe against
        # itself. Anything else -- a page that only partly overlaps prior
        # commits -- gets those specific already-covered rows silently
        # dropped instead of the whole page being rejected.
        already_covered = set() if exact_match else _covered_row_indices(progress["committed_ranges"], lo, hi)

        page = _fetch_rows_json(req.dataset, req.config, req.split, lo, req.page_size).get("rows", [])
        if not page:
            raise HTTPException(status_code=404, detail=f"no rows found at offset {lo} for {req.dataset}")
        features = _fetch_features(req.dataset, req.config)

        edits = {int(k): v for k, v in req.edits.items()}
        deletes = {int(i) for i in req.deletes}

        def build_row(item):
            row_idx = item["row_idx"]
            if row_idx in deletes or row_idx in already_covered:
                return None
            row = dict(item["row"])
            if row_idx in edits:
                row[req.text_field] = edits[row_idx]
            audio_url = _audio_url(row.get(req.audio_field))
            if audio_url:
                row[req.audio_field] = {"bytes": _download_bytes(audio_url), "path": None}
            return row

        # Downloading ~100 small audio files sequentially is the slow part --
        # parallelize it. This is the only real network cost of a commit now.
        with ThreadPoolExecutor(max_workers=12) as pool:
            built = list(pool.map(build_row, page))
        rows = [r for r in built if r is not None]

        if not rows:
            # Nothing new -- every row in this range was either deleted or
            # already written in an earlier shard. Don't push an empty file.
            if not exact_match and already_covered:
                progress["committed_ranges"] = _merge_ranges(progress["committed_ranges"] + [[lo, hi]])
                _write_progress(repo_id, req.language, progress)
            return {
                "ok": True,
                "target_repo": repo_id,
                "shard_path": None,
                "rows_written": 0,
                "rows_edited": 0,
                "rows_deleted": len(deletes),
                "rows_skipped_duplicate": len(already_covered),
                "recommit": exact_match,
            }

        from datasets import Dataset
        ds = Dataset.from_list(rows, features=features)

        shard_name = f"train-{lo:06d}-{hi - 1:06d}.parquet"
        path_in_repo = f"data/{shard_name}"

        buf = io.BytesIO()
        ds.to_parquet(buf)
        buf.seek(0)

        api.upload_file(
            path_or_fileobj=buf,
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"review page {lo}-{hi - 1} ({len(edits)} edits, {len(deletes)} deletes, "
                            f"{len(already_covered)} deduped)",
        )

        if not exact_match:
            progress["committed_ranges"] = _merge_ranges(progress["committed_ranges"] + [[lo, hi]])
            _write_progress(repo_id, req.language, progress)

    return {
        "ok": True,
        "target_repo": repo_id,
        "shard_path": path_in_repo,
        "rows_written": len(ds),
        "rows_edited": len(edits),
        "rows_deleted": len(deletes),
        "rows_skipped_duplicate": len(already_covered),
        "recommit": exact_match,
    }
