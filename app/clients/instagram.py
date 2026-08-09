"""Listing des videos d'un compte Instagram, via l'API web et la session dediee.

Pourquoi ce module existe : l'extracteur de profil de yt-dlp (`instagram:user`)
est casse -- Instagram a change son API et yt-dlp repond "Unable to extract
data" sur tous les comptes. En revanche :

- l'API web d'Instagram repond correctement des lors qu'on lui presente une
  session connectee (celle du navigateur de scraping dedie) ;
- yt-dlp reste parfaitement fonctionnel sur une publication *individuelle*, et
  y recupere une meilleure qualite que les URLs directes du feed.

On combine donc les deux : listing ici, telechargement par yt-dlp.

Deux endpoints sont utilises :
  1. `users/web_profile_info` -> identifiant numerique du compte
  2. `feed/user/<id>`         -> publications, paginees via `max_id`
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar

import httpx

from .. import browser
from ..models import FailureKind, Platform, PipelineError, ScrapeParams

API = "https://www.instagram.com/api/v1"

# Identifiant public du client web Instagram, requis par ces endpoints.
_WEB_APP_ID = "936619743392459"

_HEADERS = {
    "x-ig-app-id": _WEB_APP_ID,
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.instagram.com/",
}

PAGE_SIZE = 12
# Pause entre deux pages : Instagram limite vite les enchainements trop rapides.
PAGE_DELAY_S = 1.5
MAX_PAGES = 20


def _cookies() -> dict[str, str]:
    """Cookies Instagram de la session de scraping dediee."""
    path = browser.cookies_path()
    if not path.exists():
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            "Aucune session capturee. Ouvre Parametres > Navigateur de scraping, "
            "connecte le compte dedie, puis clique sur « Capturer la session ».",
        )
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    cookies = {c.name: c.value for c in jar if "instagram" in (c.domain or "")}
    if "sessionid" not in cookies:
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            "La session capturee ne contient pas de cookie Instagram valide. "
            "Connecte le compte dedie dans le navigateur de scraping, puis "
            "recapture la session.",
        )
    return cookies


def _check(resp: httpx.Response, context: str) -> None:
    if resp.status_code == 200:
        return
    if resp.status_code in (401, 403):
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            f"Instagram a refuse la session ({resp.status_code}) sur {context}. "
            f"Elle a probablement expire : recapture-la depuis Parametres.",
        )
    if resp.status_code == 404:
        raise PipelineError(
            FailureKind.INVALID_INPUT, f"Compte introuvable ({context})."
        )
    if resp.status_code == 429:
        raise PipelineError(
            FailureKind.QUOTA,
            "Instagram limite les requetes (429). Attends quelques minutes.",
        )
    raise PipelineError(
        FailureKind.TRANSIENT,
        f"Instagram a repondu {resp.status_code} sur {context} : {resp.text[:200]}",
    )


async def _user_id(client: httpx.AsyncClient, handle: str) -> str:
    resp = await client.get(
        f"{API}/users/web_profile_info/", params={"username": handle}
    )
    _check(resp, f"@{handle}")
    try:
        user = resp.json()["data"]["user"]
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            f"Reponse inattendue d'Instagram pour @{handle}.",
        ) from exc
    if user.get("is_private"):
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            f"@{handle} est un compte prive. Le compte de scraping doit le suivre "
            f"pour y acceder.",
        )
    return str(user["id"])


def _first_url(candidates) -> str | None:
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            return first.get("url")
    return None


def normalize(item: dict, handle: str) -> dict | None:
    """Convertit une publication Instagram vers le schema interne."""
    if not item.get("video_versions"):
        return None  # photo ou carrousel sans video

    code = item.get("code")
    if not code:
        return None

    page_url = f"https://www.instagram.com/reel/{code}/"

    def _int(*keys: str) -> int:
        for key in keys:
            val = item.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
        return 0

    duration = item.get("video_duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    taken_at = item.get("taken_at")
    posted_at = None
    if taken_at:
        try:
            posted_at = datetime.fromtimestamp(
                float(taken_at), tz=timezone.utc
            ).isoformat()
        except (OverflowError, OSError, ValueError, TypeError):
            posted_at = None

    caption = item.get("caption") or {}
    text = caption.get("text") if isinstance(caption, dict) else ""

    thumb = _first_url((item.get("image_versions2") or {}).get("candidates"))

    # Le telechargement repasse par yt-dlp : il choisit le meilleur format et
    # obtient une qualite superieure aux URLs directes du feed.
    from .ytdlp import YTDLP_SCHEME

    return {
        "platform": str(Platform.INSTAGRAM),
        "account": (item.get("user") or {}).get("username") or handle,
        "external_id": str(code),
        "post_url": page_url,
        "source_url": f"{YTDLP_SCHEME}{page_url}",
        "caption": (text or "")[:2000],
        "thumbnail_url": thumb,
        "view_count": _int("play_count", "ig_play_count", "view_count"),
        "like_count": _int("like_count"),
        "posted_at": posted_at,
        "duration_s": duration,
        "width": _int("original_width"),
        "height": _int("original_height"),
    }


async def scrape_account(handle: str, params: ScrapeParams) -> list[dict]:
    """Liste les videos d'un compte, en paginant jusqu'au quota demande."""
    handle = handle.strip().lstrip("@")
    if not handle:
        return []

    cookies = _cookies()
    wanted = params.max_videos_per_account
    out: list[dict] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(
        headers=_HEADERS, cookies=cookies, timeout=httpx.Timeout(45.0, connect=20.0),
        follow_redirects=True,
    ) as client:
        user_id = await _user_id(client, handle)

        max_id: str | None = None
        pages = 0
        while len(out) < wanted and pages < MAX_PAGES:
            query: dict = {"count": PAGE_SIZE}
            if max_id:
                query["max_id"] = max_id

            resp = await client.get(f"{API}/feed/user/{user_id}/", params=query)
            _check(resp, f"feed de @{handle}")
            data = resp.json()
            items = data.get("items") or []
            pages += 1

            for item in items:
                norm = normalize(item, handle)
                if not norm or norm["external_id"] in seen:
                    continue
                if params.min_views and norm["view_count"] < params.min_views:
                    continue
                if params.posted_after and norm["posted_at"]:
                    if norm["posted_at"][:10] < params.posted_after:
                        continue
                seen.add(norm["external_id"])
                out.append(norm)
                if len(out) >= wanted:
                    break

            if len(out) >= wanted or not data.get("more_available"):
                break
            max_id = data.get("next_max_id")
            if not max_id:
                break
            await asyncio.sleep(PAGE_DELAY_S)

    print(
        f"[instagram] @{handle} : {pages} page(s) parcourue(s), "
        f"{len(out)} video(s) retenue(s).",
        flush=True,
    )
    return out
