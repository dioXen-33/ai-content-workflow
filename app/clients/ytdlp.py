"""Backend de scraping gratuit, base sur yt-dlp.

Aucune API payante : yt-dlp lit directement les pages publiques. Deux nuances
selon la plateforme :

- TikTok    : fonctionne sans session.
- Instagram : exige une session connectee. On utilise les cookies du navigateur
              de scraping dedie (voir app/browser.py), jamais ceux du profil
              personnel.

On appelle yt-dlp via son API Python plutot qu'en sous-processus : le processus
principal active `truststore` (voir app/__init__.py), donc la validation TLS
passe par le magasin du systeme. C'est indispensable derriere un antivirus ou un
proxy qui inspecte le HTTPS -- un sous-processus, lui, n'heriterait pas de ce
reglage et echouerait en CERTIFICATE_VERIFY_FAILED.

Le listing et le telechargement passent tous deux par yt-dlp : les URLs de media
sont signees et exigent les memes en-tetes et cookies, donc les retelecharger
nous-memes echouerait.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import httpx

from .. import browser
from ..media import UA, TIKTOK_SCHEME
from ..models import FailureKind, Platform, PipelineError, ScrapeParams

# Marqueur : indique au pipeline qu'il faut telecharger via yt-dlp et non en
# HTTP direct. Meme principe que `dryrun://` pour le mode simulation.
YTDLP_SCHEME = "ytdlp://"


def available() -> bool:
    try:
        import yt_dlp  # noqa: F401

        return True
    except ImportError:
        return False


def _cookie_file(platform: Platform) -> str | None:
    """Cookies de la session dediee, obligatoires pour Instagram."""
    path = browser.cookies_path()
    if path.exists():
        return str(path)
    if platform == Platform.INSTAGRAM:
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            "Instagram exige une session connectee et aucun cookie n'a ete "
            "capture. Ouvre Parametres > Navigateur de scraping, connecte le "
            "compte dedie, puis clique sur « Capturer la session ».",
        )
    return None


def _profile_url(platform: Platform, handle: str) -> str:
    if platform == Platform.INSTAGRAM:
        return f"https://www.instagram.com/{handle}/"
    if platform == Platform.PINTEREST:
        # La cible Pinterest est une URL complete, ou un chemin
        # `utilisateur/tableau` saisi apres le prefixe `pinterest:`.
        if handle.startswith(("http://", "https://")):
            return handle
        if handle.startswith("www.") or "pinterest." in handle or "pin.it/" in handle:
            return f"https://{handle}"
        return f"https://www.pinterest.com/{handle.strip('/')}/"
    return f"https://www.tiktok.com/@{handle}"


_SECUID_RE = re.compile(r'"secUid":"([^"]{20,})"')


async def _tiktok_sec_uid(handle: str) -> str | None:
    """Recupere l'identifiant interne (`secUid`) d'un compte TikTok.

    L'extracteur de profil de yt-dlp lit cet identifiant dans la page, et
    echoue en « Unable to extract secondary user ID » quand TikTok sert une
    version allegee -- ce qui arrive typiquement depuis une IP de datacenter.
    On va donc le chercher nous-memes, avec les cookies de la session dediee et
    des en-tetes de navigateur credibles, pour le passer ensuite a yt-dlp sous
    la forme `tiktokuser:<secUid>`.
    """
    jar = None
    path = browser.cookies_path()
    if path.exists():
        jar = MozillaCookieJar(str(path))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except OSError:
            jar = None

    cookies = (
        {c.name: c.value for c in jar if "tiktok" in (c.domain or "")} if jar else {}
    )
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(30.0, connect=15.0)
        ) as client:
            resp = await client.get(
                f"https://www.tiktok.com/@{handle}", headers=headers, cookies=cookies
            )
        if resp.status_code != 200:
            return None
        found = _SECUID_RE.search(resp.text)
        return found.group(1) if found else None
    except httpx.HTTPError:
        return None


async def _resolve_short_link(url: str) -> str:
    """Suit une redirection `pin.it`.

    Les liens de partage Pinterest sont raccourcis, et le domaine `pin.it` ne
    correspond a aucun extracteur yt-dlp : il faut retrouver l'URL canonique du
    pin avant de la lui passer.
    """
    if "pin.it/" not in url.lower():
        return url
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(20.0, connect=10.0)
        ) as client:
            resp = await client.get(url, headers={"User-Agent": UA})
            return str(resp.url)
    except httpx.HTTPError as exc:
        raise PipelineError(
            FailureKind.TRANSIENT, f"Lien pin.it non resolu : {exc}"
        ) from exc


def _pinterest_label(url: str) -> str:
    """Etiquette courte d'une cible Pinterest, pour l'affichage dans l'UI."""
    path = url.split("://", 1)[-1]
    path = path.split("/", 1)[1] if "/" in path else path
    path = path.split("?")[0].strip("/")
    return path[:60] or "pinterest"


def _classify(message: str) -> FailureKind:
    low = message.lower()
    if any(k in low for k in ("login required", "requires authentication",
                              "rate-limit", "rate limit", "429",
                              "please log in", "empty media response",
                              "checkpoint")):
        return FailureKind.QUOTA
    if any(k in low for k in ("private", "not found", "unavailable", "404",
                              "does not exist")):
        return FailureKind.INVALID_INPUT
    if any(k in low for k in ("timed out", "connection", "temporary",
                              "certificate")):
        return FailureKind.TRANSIENT
    return FailureKind.UNKNOWN


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _to_iso(value) -> str | None:
    if not value:
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        # yt-dlp fournit parfois `upload_date` au format AAAAMMJJ.
        text = str(value)
        if len(text) == 8 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}-{text[6:]}T00:00:00+00:00"
        return None
    if ts > 1e11:
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def normalize(entry: dict, platform: Platform, account: str) -> dict | None:
    """Convertit une entree yt-dlp vers le schema interne."""
    video_id = entry.get("id")
    if not video_id:
        return None

    # On conserve l'URL de la page : le telechargement repassera par yt-dlp,
    # qui reconstruira une URL de media valide au moment voulu.
    page_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if not page_url:
        return None

    duration = entry.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None

    def _int(*keys: str) -> int:
        for key in keys:
            val = entry.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
        return 0

    thumbnail = entry.get("thumbnail")
    if not thumbnail:
        thumbs = entry.get("thumbnails") or []
        if isinstance(thumbs, list) and thumbs and isinstance(thumbs[-1], dict):
            thumbnail = thumbs[-1].get("url")

    # Sur Pinterest, `uploader_id` est un identifiant numerique illisible : on
    # prefere le nom du createur, puis l'etiquette de la cible.
    if platform == Platform.PINTEREST:
        who = entry.get("uploader") or account
    else:
        who = entry.get("uploader_id") or entry.get("uploader") or account

    return {
        "platform": str(platform),
        "account": str(who)[:60],
        "external_id": str(video_id),
        "post_url": page_url,
        "source_url": (
            f"{TIKTOK_SCHEME}{page_url}" if platform == Platform.TIKTOK
            else f"{YTDLP_SCHEME}{page_url}"
        ),
        "caption": (entry.get("description") or entry.get("title") or "")[:2000],
        "thumbnail_url": thumbnail,
        "view_count": _int("view_count", "play_count"),
        "like_count": _int("like_count"),
        "posted_at": _to_iso(entry.get("timestamp") or entry.get("upload_date")),
        "duration_s": duration,
        "width": _int("width"),
        "height": _int("height"),
    }


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _extract_sync(url: str, opts: dict) -> dict:
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


async def scrape_account(
    platform: Platform, handle: str, params: ScrapeParams
) -> list[dict]:
    handle = handle.strip().lstrip("@")
    if not handle:
        return []

    # Instagram : l'extracteur de profil de yt-dlp est casse ("Unable to extract
    # data"). On passe par l'API web avec la session dediee ; le telechargement,
    # lui, reste confie a yt-dlp qui fonctionne sur une publication isolee.
    if platform == Platform.INSTAGRAM:
        from . import instagram

        return await instagram.scrape_account(handle, params)

    if not available():
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            "yt-dlp n'est pas installe. Lance : "
            ".venv\\Scripts\\python.exe -m pip install yt-dlp",
        )

    url = _profile_url(platform, handle)
    label = handle

    if platform == Platform.PINTEREST:
        url = await _resolve_short_link(url)
        label = _pinterest_label(url)

    # Un tableau Pinterest melange images et videos, et les pins image echouent
    # a l'extraction (aucun format video). Se limiter aux N premiers elements
    # ramenerait souvent zero video : on ouvre large et on coupe apres coup.
    window = params.max_videos_per_account
    if platform == Platform.PINTEREST:
        window = min(max(window * 10, 100), 500)

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        # Limite le nombre d'elements parcourus : evite de derouler tout un
        # profil et de se faire limiter par la plateforme.
        "playlist_items": f"1:{window}",
        "extractor_args": {"instagram": {"include_stories": ["false"]}},
    }
    cookies = _cookie_file(platform)
    if cookies:
        opts["cookiefile"] = cookies
    if params.posted_after:
        opts["daterange"] = None  # filtre applique manuellement plus bas

    async def _extract(target: str) -> dict:
        return await asyncio.to_thread(_extract_sync, target, opts)

    def _entries(info: dict) -> list[dict]:
        ents = (info or {}).get("entries")
        if ents is None:
            ents = [info] if info and info.get("id") else []
        return [e for e in ents if isinstance(e, dict)]

    # Extraction directe. `ignoreerrors=True` fait que yt-dlp ne LEVE PAS quand
    # il echoue a lire un profil : il logue et renvoie un resultat vide. Il faut
    # donc traiter « leve » et « renvoie vide » de la meme facon.
    try:
        entries = _entries(await _extract(url))
    except Exception as exc:  # yt_dlp leve ses propres types
        if platform != Platform.TIKTOK:
            raise PipelineError(
                _classify(str(exc)),
                f"yt-dlp a echoue sur @{handle} ({platform}) : {str(exc)[:300]}",
            ) from exc
        entries = []  # le repli secUid ci-dessous prend le relais

    # TikTok : l'extracteur de profil de yt-dlp echoue souvent a lire
    # l'identifiant interne du compte (« Unable to extract secondary user ID »),
    # que la requete leve ou renvoie simplement du vide. On resout cet
    # identifiant nous-memes et on relance sur `tiktokuser:<secUid>`, la forme
    # que yt-dlp accepte directement.
    if platform == Platform.TIKTOK and not entries:
        # 1. Repli secUid via yt-dlp : marche quand TikTok sert encore ses
        #    donnees a une requete directe (typiquement une IP residentielle).
        sec_uid = await _tiktok_sec_uid(handle)
        if sec_uid:
            print(f"[yt-dlp] @{handle} : repli sur tiktokuser:<secUid>.", flush=True)
            try:
                entries = _entries(await _extract(f"tiktokuser:{sec_uid}"))
            except Exception:
                entries = []  # l'API item_list a renvoye du vide : etape suivante

        # 2. Recolte par navigateur : TikTok signe alors ses propres requetes,
        #    ce qui contourne le blocage des requetes directes. C'est la seule
        #    voie fiable depuis une IP filtree (serveur).
        if not entries and await browser._cdp_reachable():
            from . import tiktok_browser

            print(f"[yt-dlp] @{handle} : bascule sur la recolte par navigateur.",
                  flush=True)
            return await tiktok_browser.harvest(handle, params)

        if not entries:
            raise PipelineError(
                FailureKind.QUOTA,
                f"TikTok bloque le listing de @{handle} par requete directe. "
                f"Ouvre le navigateur de scraping (Parametres > « Connexion "
                f"TikTok ») et relance : un vrai navigateur contourne le blocage.",
            )

    if not entries:
        if platform == Platform.PINTEREST:
            raise PipelineError(
                FailureKind.INVALID_INPUT,
                f"Aucune video trouvee sur {label}. Verifie qu'il s'agit bien "
                f"d'un tableau (pinterest.com/utilisateur/tableau) ou d'un pin : "
                f"un profil Pinterest nu n'est pas exploitable, et un tableau "
                f"uniquement compose d'images ne donne aucune video.",
            )
        raise PipelineError(
            FailureKind.QUOTA,
            f"yt-dlp n'a trouve aucune video pour @{handle} ({platform}). "
            f"Compte prive, session expiree, ou plateforme qui limite l'acces.",
        )

    out_videos: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        norm = normalize(entry, platform, label)
        if not norm or norm["external_id"] in seen:
            continue
        # Pinterest n'expose aucun compteur de vues : appliquer le filtre y
        # ecarterait tout. Les autres plateformes le renseignent.
        if (params.min_views and platform != Platform.PINTEREST
                and norm["view_count"] < params.min_views):
            continue
        if params.posted_after and norm["posted_at"]:
            if norm["posted_at"][:10] < params.posted_after:
                continue
        seen.add(norm["external_id"])
        out_videos.append(norm)
        if len(out_videos) >= params.max_videos_per_account:
            break

    print(
        f"[yt-dlp] {platform} {label} : {len(entries)} entree(s), "
        f"{len(out_videos)} video(s) retenue(s).",
        flush=True,
    )
    return out_videos


# ---------------------------------------------------------------------------
# Telechargement
# ---------------------------------------------------------------------------


def _download_sync(url: str, opts: dict) -> None:
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


async def download(page_url: str, dest: Path) -> Path:
    """Telecharge une video depuis l'URL de sa page, via yt-dlp."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    low = page_url.lower()
    if "instagram.com" in low:
        platform = Platform.INSTAGRAM       # seule plateforme a exiger la session
    elif "pinterest." in low or "pin.it/" in low:
        platform = Platform.PINTEREST
    else:
        platform = Platform.TIKTOK

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        # Priorise un MP4 lisible directement par ffmpeg et Kling.
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest),
        "overwrites": True,
    }
    cookies = _cookie_file(platform)
    if cookies:
        opts["cookiefile"] = cookies

    try:
        await asyncio.to_thread(_download_sync, page_url, opts)
    except Exception as exc:
        message = str(exc)
        raise PipelineError(
            _classify(message), f"Telechargement yt-dlp echoue : {message[:300]}"
        ) from exc

    if not dest.exists() or dest.stat().st_size == 0:
        raise PipelineError(
            FailureKind.UNKNOWN,
            f"yt-dlp n'a produit aucun fichier pour {page_url}.",
        )
    return dest
