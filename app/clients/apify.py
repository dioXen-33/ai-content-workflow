"""Client Apify : decouverte des videos d'un compte Instagram / TikTok.

Strategie de cout : Apify facture au resultat, pas au gigaoctet. On l'utilise
uniquement pour obtenir la liste des posts et leurs URLs CDN, puis on telecharge
les binaires nous-memes (gratuit). C'est nettement moins cher que l'option
"download" integree des acteurs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import settings
from ..media import DRYRUN_SCHEME
from ..models import FailureKind, Platform, PipelineError, ScrapeParams

API = "https://api.apify.com/v2"
POLL_INTERVAL = 5.0
MAX_WAIT = 900.0


# ---------------------------------------------------------------------------
# Construction de l'entree des acteurs
#
# Chaque acteur Apify a son propre schema d'entree et valide les champs inconnus.
# On maintient donc une table par acteur. Si tu changes d'acteur dans .env,
# ajoute son schema ici, sinon le fallback generique s'applique.
# ---------------------------------------------------------------------------


def _instagram_input(actor: str, handle: str, p: ScrapeParams) -> dict:
    url = f"https://www.instagram.com/{handle}/"
    if actor.startswith("apidojo~"):
        # Le profil racine renvoie posts + reels. Champs valides de cet acteur :
        # startUrls, maxItems, until (pas de `includeVideos`).
        payload: dict[str, Any] = {
            "startUrls": [url],
            "maxItems": p.max_videos_per_account,
        }
        if p.posted_after:
            payload["until"] = p.posted_after
        return payload
    if actor.startswith("apify~instagram-reel-scraper"):
        return {"username": [handle], "resultsLimit": p.max_videos_per_account}
    # apify~instagram-scraper et compatibles
    payload: dict[str, Any] = {
        "directUrls": [url],
        "resultsType": "posts",
        "resultsLimit": p.max_videos_per_account,
        "searchType": "user",
    }
    if p.posted_after:
        payload["onlyPostsNewerThan"] = p.posted_after
    return payload


def _tiktok_input(actor: str, handle: str, p: ScrapeParams) -> dict:
    payload: dict[str, Any] = {
        "profiles": [handle],
        "resultsPerPage": p.max_videos_per_account,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSubtitles": False,
        "shouldDownloadSlideshowImages": False,
    }
    if p.posted_after:
        payload["oldestPostDateUnified"] = p.posted_after
    return payload


def build_input(platform: Platform, actor: str, handle: str, p: ScrapeParams) -> dict:
    if platform == Platform.INSTAGRAM:
        return _instagram_input(actor, handle, p)
    return _tiktok_input(actor, handle, p)


# ---------------------------------------------------------------------------
# Normalisation des resultats
#
# Les acteurs ne renvoient pas les memes noms de champs. On essaie une liste de
# chemins candidats pour chaque information.
# ---------------------------------------------------------------------------


def _pick(data: dict, *paths: str) -> Any:
    for path in paths:
        cur: Any = data
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit() and len(cur) > int(part):
                cur = cur[int(part)]
            else:
                cur = None
            if cur is None:
                break
        if cur not in (None, "", [], {}):
            return cur
    return None


def _to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return value
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:  # millisecondes
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize(item: dict, platform: Platform, account: str) -> dict | None:
    """Ramene un resultat d'acteur au schema interne. None si inexploitable."""

    # On cherche l'URL du fichier video parmi tous les noms de champ rencontres.
    # Instagram (apidojo) : `video.url`. TikTok : versions sans filigrane d'abord
    # -- le logo et le pseudo incrustes se retrouveraient sinon sur la premiere
    # frame, et Nano Banana Pro les regenererait en charabia.
    source_url = _pick(
        item,
        # --- TikTok, sans filigrane en priorite ---
        "videoUrlNoWaterMark",
        "video.playAddr",
        "video.downloadAddr",
        "videoMeta.downloadAddr",
        "downloadAddr",
        "playAddr",
        # --- Instagram (apidojo) ---
        "video.url",
        "videoUrl",
        "videoUrlWithWaterMark",
        "mediaUrls.0",
        # --- carrousels / medias multiples ---
        "videos.0.url",
        "media.0.video.url",
    )
    if not source_url:
        return None
    if isinstance(source_url, list):
        source_url = source_url[0] if source_url else None
    if not isinstance(source_url, str) or not source_url.startswith("http"):
        return None

    external_id = _pick(
        item, "id", "code", "shortCode", "shortcode", "postId", "awemeId", "videoId"
    )
    if not external_id:
        return None

    duration = _pick(
        item, "video.duration", "videoDuration", "videoMeta.duration", "duration"
    )
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    def _int(*paths: str) -> int:
        val = _pick(item, *paths)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    thumbnail = _pick(
        item,
        "image.url", "displayUrl", "thumbnailUrl",
        "videoMeta.coverUrl", "covers.0", "cover",
    )
    if isinstance(thumbnail, list):
        thumbnail = thumbnail[0] if thumbnail else None

    return {
        "platform": str(platform),
        "account": _pick(item, "owner.username") or account,
        "external_id": str(external_id),
        "post_url": _pick(item, "url", "webVideoUrl", "postPage", "shareUrl"),
        "source_url": source_url,
        "caption": (_pick(item, "caption", "text", "title", "description") or "")[:2000],
        "thumbnail_url": thumbnail,
        "view_count": _int(
            "video.playCount", "videoPlayCount", "playCount", "views", "viewCount"
        ),
        "like_count": _int("likeCount", "likesCount", "diggCount", "likes"),
        "posted_at": _to_iso(
            _pick(item, "createdAt", "timestamp", "createTimeISO",
                  "createTime", "takenAtTimestamp")
        ),
        "duration_s": duration,
        "width": _int("video.width", "videoMeta.width", "dimensionsWidth", "width"),
        "height": _int("video.height", "videoMeta.height", "dimensionsHeight", "height"),
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _classify(status_code: int, body: str) -> FailureKind:
    if status_code in (401, 403):
        return FailureKind.INVALID_INPUT
    if status_code == 402 or "credit" in body.lower() or "quota" in body.lower():
        return FailureKind.QUOTA
    if status_code == 429 or status_code >= 500:
        return FailureKind.TRANSIENT
    if 400 <= status_code < 500:
        return FailureKind.INVALID_INPUT
    return FailureKind.UNKNOWN


async def _start_run(client: httpx.AsyncClient, actor: str, payload: dict) -> dict:
    resp = await client.post(
        f"{API}/acts/{actor}/runs",
        params={"token": settings.apify_token},
        json=payload,
    )
    if resp.status_code >= 400:
        raise PipelineError(
            _classify(resp.status_code, resp.text),
            f"Apify a refuse le lancement de {actor} ({resp.status_code}) : "
            f"{resp.text[:400]}",
        )
    return resp.json()["data"]


async def _wait_run(client: httpx.AsyncClient, run_id: str) -> dict:
    waited = 0.0
    while waited < MAX_WAIT:
        resp = await client.get(
            f"{API}/actor-runs/{run_id}", params={"token": settings.apify_token}
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        status = data.get("status")
        if status == "SUCCEEDED":
            return data
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise PipelineError(
                FailureKind.UNKNOWN,
                f"Le run Apify s'est termine en {status}. "
                f"Detail : https://console.apify.com/actors/runs/{run_id}",
            )
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
    raise PipelineError(
        FailureKind.TRANSIENT, f"Le run Apify {run_id} depasse {MAX_WAIT:.0f} s."
    )


async def _fetch_items(client: httpx.AsyncClient, dataset_id: str) -> list[dict]:
    resp = await client.get(
        f"{API}/datasets/{dataset_id}/items",
        params={"token": settings.apify_token, "clean": "true", "format": "json"},
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


async def scrape_account(
    platform: Platform, handle: str, params: ScrapeParams
) -> list[dict]:
    """Scrape un compte et renvoie les videos normalisees."""
    handle = handle.strip().lstrip("@")
    if not handle:
        return []

    if settings.dry_run:
        return _fake_results(platform, handle, params)

    if not settings.apify_token:
        raise PipelineError(FailureKind.INVALID_INPUT, "APIFY_TOKEN manquant.")

    actor = (
        settings.apify_instagram_actor
        if platform == Platform.INSTAGRAM
        else settings.apify_tiktok_actor
    )
    payload = build_input(platform, actor, handle, params)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
        run = await _start_run(client, actor, payload)
        run_url = f"https://console.apify.com/actors/runs/{run['id']}"
        print(
            f"[apify] {actor} @{handle} : run lance, inspection brute -> {run_url}",
            flush=True,
        )
        finished = await _wait_run(client, run["id"])
        items = await _fetch_items(client, finished["defaultDatasetId"])

    # Sentinelle `noResults` : plusieurs acteurs "Pay Per Result" (dont
    # apidojo) la renvoient quand le plan Apify gratuit tente un appel via API.
    # On la detecte pour donner un message clair plutot qu'un silencieux "0".
    if items and all(
        isinstance(it, dict) and set(it.keys()) <= {"noResults"} for it in items
    ):
        raise PipelineError(
            FailureKind.QUOTA,
            f"L'acteur {actor} a renvoye 'noResults' : ce scraper exige un plan "
            f"Apify payant pour l'usage via API (le plan gratuit ne peut pas "
            f"l'appeler par API). Passe sur un plan payant Apify, ou change "
            f"d'acteur dans .env.",
        )

    out: list[dict] = []
    seen: set[str] = set()
    dropped_no_video = 0
    for item in items:
        norm = normalize(item, platform, handle)
        if norm is None:
            dropped_no_video += 1
            continue
        if norm["external_id"] in seen:
            continue
        if params.min_views and norm["view_count"] < params.min_views:
            continue
        seen.add(norm["external_id"])
        out.append(norm)

    # Diagnostic console : si l'acteur a renvoye des items mais qu'on n'en garde
    # aucun, c'est presque toujours un champ mal mappe dans normalize(). On logue
    # les cles du premier item brut pour rendre la correction immediate.
    if items and not out:
        sample_keys = sorted(items[0].keys())[:40]
        # On sauvegarde l'item brut complet : la structure imbriquee (video.*,
        # etc.) ne se voit pas dans la liste des cles de surface.
        try:
            import json

            dbg = settings.data_path / "apify_last_raw_item.json"
            dbg.write_text(
                json.dumps(items[0], indent=2, ensure_ascii=False), encoding="utf-8"
            )
            dbg_note = f" | 1er item brut ecrit dans {dbg}"
        except Exception:
            dbg_note = ""
        print(
            f"[apify] {actor} @{handle} : {len(items)} item(s) recu(s), 0 retenu "
            f"({dropped_no_video} sans URL video). Cles du 1er item : "
            f"{sample_keys}{dbg_note}",
            flush=True,
        )
    else:
        print(
            f"[apify] {actor} @{handle} : {len(items)} recu(s), {len(out)} video(s) "
            f"retenue(s).",
            flush=True,
        )
    return out


def _fake_results(platform: Platform, handle: str, params: ScrapeParams) -> list[dict]:
    """Jeu de donnees factice pour le mode dry-run.

    Les `source_url` utilisent le schema `dryrun://` : le pipeline fabrique alors
    la video localement avec ffmpeg au lieu de la telecharger. Aucun reseau,
    aucun credit.
    """
    n = min(params.max_videos_per_account, 4)
    return [
        {
            "platform": str(platform),
            "account": handle,
            "external_id": f"dry-{handle}-{i}",
            "post_url": f"https://example.invalid/{handle}/{i}",
            "source_url": f"{DRYRUN_SCHEME}{i}",
            "caption": f"[dry-run] video de test {i + 1} de @{handle}",
            "thumbnail_url": None,
            "view_count": 100_000 - i * 1000,
            "like_count": 5_000 - i * 100,
            "posted_at": datetime.now(tz=timezone.utc).isoformat(),
            "duration_s": 12.0,
            "width": 720,
            "height": 1280,
        }
        for i in range(n)
    ]
