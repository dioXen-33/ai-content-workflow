"""Scraping TikTok via le navigateur isole (Chrome DevTools Protocol).

Pourquoi ce module existe : l'API de liste de TikTok (`/api/post/item_list/`)
exige une signature calculee par son JavaScript anti-bot. Aucune requete HTTP
directe -- ni yt-dlp, ni httpx -- ne peut la produire, et TikTok repond alors un
corps vide (HTTP 200 sans contenu).

La parade : laisser un VRAI navigateur charger le profil. TikTok signe alors
ses propres requetes, et on intercepte leurs reponses via le DevTools Protocol.
Le navigateur utilise est celui, isole, de app/browser.py -- avec la session
TikTok dediee si elle a ete capturee.

Le telechargement de chaque video reste confie a yt-dlp (une publication isolee
n'a pas le meme verrou qu'un listing).
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone

import httpx

from .. import browser
from ..config import settings
from ..models import FailureKind, Platform, PipelineError, ScrapeParams
from ..media import TIKTOK_SCHEME

# Endpoint que TikTok appelle pour paginer la liste des videos d'un profil.
_ITEM_LIST = "/api/post/item_list/"

# Bornes de securite : on ne deroule pas un profil indefiniment.
_MAX_SCROLLS = 40
_SCROLL_PAUSE_S = 1.6
_OVERALL_TIMEOUT_S = 180.0


class _CDPPage:
    """Session DevTools sur un onglet : commandes et evenements multiplexes."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        fut.set_result(msg)
                elif "method" in msg:
                    await self.events.put(msg)
        except Exception:
            pass

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(
            json.dumps({"id": mid, "method": method, "params": params or {}})
        )
        msg = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in msg:
            raise PipelineError(FailureKind.UNKNOWN, f"CDP {method} : {msg['error']}")
        return msg.get("result") or {}

    def close(self) -> None:
        self._reader.cancel()


async def _open_page(url: str):
    """Cree un onglet sur `url` et ouvre une session DevTools dessus."""
    import websockets

    base = f"http://127.0.0.1:{settings.browser_debug_port}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.put(f"{base}/json/new?{url}")
        if resp.status_code >= 400:
            resp = await client.get(f"{base}/json/new?{url}")
        if resp.status_code >= 400:
            raise PipelineError(
                FailureKind.TRANSIENT,
                "Impossible d'ouvrir un onglet dans le navigateur de scraping.",
            )
        target = resp.json()

    ws_url = target.get("webSocketDebuggerUrl")
    tab_id = target.get("id")
    if not ws_url:
        raise PipelineError(FailureKind.TRANSIENT, "Onglet sans endpoint DevTools.")
    ws = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)
    return _CDPPage(ws), ws, tab_id


async def _close_tab(tab_id: str) -> None:
    base = f"http://127.0.0.1:{settings.browser_debug_port}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(f"{base}/json/close/{tab_id}")
    except httpx.HTTPError:
        pass


# ---------------------------------------------------------------------------
# Normalisation d'une entree item_list vers le schema interne
# ---------------------------------------------------------------------------


def _normalize(item: dict, handle: str) -> dict | None:
    video_id = item.get("id")
    if not video_id:
        return None

    author = (item.get("author") or {}).get("uniqueId") or handle
    page_url = f"https://www.tiktok.com/@{author}/video/{video_id}"

    video = item.get("video") or {}
    stats = item.get("stats") or item.get("statsV2") or {}

    def _int(source: dict, *keys: str) -> int:
        for key in keys:
            val = source.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
        return 0

    duration = video.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    created = item.get("createTime")
    posted = None
    if created:
        try:
            posted = datetime.fromtimestamp(float(created), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError, TypeError):
            posted = None

    return {
        "platform": str(Platform.TIKTOK),
        "account": author,
        "external_id": str(video_id),
        "post_url": page_url,
        # Telechargement : on rejouera la page pour une URL fraiche.
        "source_url": f"{TIKTOK_SCHEME}{page_url}",
        "caption": (item.get("desc") or "")[:2000],
        "thumbnail_url": video.get("cover") or video.get("originCover"),
        "view_count": _int(stats, "playCount"),
        "like_count": _int(stats, "diggCount"),
        "posted_at": posted,
        "duration_s": duration,
        "width": _int(video, "width"),
        "height": _int(video, "height"),
    }


# ---------------------------------------------------------------------------
# Recolte
# ---------------------------------------------------------------------------


async def _drain_body(page: _CDPPage, request_id: str) -> list[dict]:
    """Recupere le corps d'une reponse item_list et en extrait les videos."""
    try:
        res = await page.call(
            "Network.getResponseBody", {"requestId": request_id}, timeout=20
        )
    except (PipelineError, asyncio.TimeoutError):
        return []
    body = res.get("body") or ""
    if res.get("base64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8", "replace")
        except Exception:
            return []
    if not body.strip():
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    return data.get("itemList") or []


def _now() -> float:
    return asyncio.get_event_loop().time()


async def harvest(handle: str, params: ScrapeParams) -> list[dict]:
    """Recolte les videos d'un profil TikTok en pilotant le navigateur."""
    handle = handle.strip().lstrip("@")
    if not handle:
        return []

    if not await browser._cdp_reachable():
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            "Le navigateur de scraping n'est pas ouvert. Ouvre-le depuis "
            "Parametres > Navigateur de scraping (bouton « Connexion TikTok »), "
            "puis relance.",
        )

    wanted = params.max_videos_per_account
    url = f"https://www.tiktok.com/@{handle}"
    page, ws, tab_id = await _open_page(url)

    collected: dict[str, dict] = {}
    seen_requests: set[str] = set()

    async def pump(deadline: float) -> int:
        """Traite les evenements reseau ; renvoie le nombre de videos ajoutees."""
        added = 0
        while len(collected) < wanted and _now() < deadline:
            try:
                ev = await asyncio.wait_for(page.events.get(), timeout=2.0)
            except asyncio.TimeoutError:
                return added
            method = ev.get("method")
            p = ev.get("params") or {}
            if method == "Network.responseReceived":
                resp_url = (p.get("response") or {}).get("url", "")
                if _ITEM_LIST in resp_url:
                    seen_requests.add(p.get("requestId"))
            elif method == "Network.loadingFinished":
                rid = p.get("requestId")
                if rid in seen_requests:
                    seen_requests.discard(rid)
                    for item in await _drain_body(page, rid):
                        norm = _normalize(item, handle)
                        if norm and norm["external_id"] not in collected:
                            collected[norm["external_id"]] = norm
                            added += 1
        return added

    try:
        await page.call("Network.enable")
        await page.call("Page.enable")

        deadline = _now() + _OVERALL_TIMEOUT_S
        # Premier lot : la navigation a deja demarre, on laisse arriver item_list.
        await pump(min(_now() + 12, deadline))

        scrolls = 0
        while len(collected) < wanted and scrolls < _MAX_SCROLLS and _now() < deadline:
            try:
                await page.call(
                    "Runtime.evaluate",
                    {"expression": "window.scrollTo(0, document.body.scrollHeight);"},
                    timeout=10,
                )
            except (PipelineError, asyncio.TimeoutError):
                break
            scrolls += 1
            added = await pump(_now() + _SCROLL_PAUSE_S)
            # Un scroll qui ne ramene plus rien = fin de la liste (ou blocage).
            if added == 0 and scrolls > 2:
                break
    finally:
        page.close()
        try:
            await ws.close()
        except Exception:
            pass
        await _close_tab(tab_id)

    videos = list(collected.values())

    if not videos:
        raise PipelineError(
            FailureKind.QUOTA,
            f"Le navigateur n'a intercepte aucune video pour @{handle}. TikTok "
            f"n'a peut-etre pas charge la liste (compte prive, page de "
            f"verification, ou blocage). Ouvre le profil manuellement dans le "
            f"navigateur de scraping pour verifier qu'il s'affiche.",
        )

    out: list[dict] = []
    for v in videos:
        if params.min_views and v["view_count"] < params.min_views:
            continue
        if params.posted_after and v["posted_at"] and v["posted_at"][:10] < params.posted_after:
            continue
        out.append(v)
        if len(out) >= wanted:
            break

    print(
        f"[tiktok-browser] @{handle} : {len(videos)} interceptee(s), "
        f"{len(out)} retenue(s).",
        flush=True,
    )
    return out


# ---------------------------------------------------------------------------
# Telechargement d'une video TikTok
# ---------------------------------------------------------------------------

import re as _re
from http.cookiejar import MozillaCookieJar as _MozillaCookieJar

_UNIVERSAL = _re.compile(
    r'id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', _re.S
)

_DL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _tiktok_cookies() -> dict:
    path = browser.cookies_path()
    if not path.exists():
        return {}
    jar = _MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except OSError:
        return {}
    return {c.name: c.value for c in jar if "tiktok" in (c.domain or "")}


def _play_addr_from_page(html: str) -> str | None:
    """Extrait l'URL de media d'une page video TikTok."""
    m = _UNIVERSAL.search(html)
    if not m:
        return None
    try:
        scope = json.loads(m.group(1))["__DEFAULT_SCOPE__"]
    except (json.JSONDecodeError, KeyError):
        return None
    item = (
        scope.get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct", {})
    )
    video = item.get("video") or {}
    return video.get("playAddr") or video.get("downloadAddr") or None


async def download(page_url: str, dest: Path) -> Path:
    """Telecharge une video TikTok depuis l'URL de sa page.

    On rejoue la page (avec les cookies de la session) pour en extraire une URL
    de media FRAICHE, puis on la telecharge directement. Rejouer la page a chaque
    fois evite le probleme d'expiration des URLs signees.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cookies = _tiktok_cookies()
    headers = {"User-Agent": _DL_UA, "Accept-Language": "fr-FR,fr;q=0.9"}

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=httpx.Timeout(60.0, connect=20.0)
    ) as client:
        page = await client.get(page_url, headers=headers, cookies=cookies)
        play = _play_addr_from_page(page.text)
        if not play:
            raise PipelineError(
                FailureKind.TRANSIENT,
                f"URL de media introuvable sur la page {page_url}. La session "
                f"TikTok a peut-etre expire, ou la video n'est plus disponible.",
            )
        dl_headers = {**headers, "Referer": "https://www.tiktok.com/", "Accept": "*/*"}
        tmp = dest.with_suffix(dest.suffix + ".part")
        async with client.stream(
            "GET", play, headers=dl_headers, cookies=cookies
        ) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)

    if not tmp.exists() or tmp.stat().st_size == 0:
        raise PipelineError(
            FailureKind.TRANSIENT, f"Telechargement vide pour {page_url}."
        )
    tmp.replace(dest)
    return dest
