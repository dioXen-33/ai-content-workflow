"""Navigateur de scraping isole, avec capture de session.

Objectif : disposer d'une session Instagram dediee au scraping, sans jamais
toucher au profil personnel de l'utilisateur.

Fonctionnement :

1. On lance le navigateur avec `--user-data-dir` pointant sur un dossier a nous.
   C'est un profil vierge et cloisonne : ni historique, ni cookies, ni comptes
   du profil habituel. L'utilisateur y connecte le compte dedie au scraping.

2. Pour recuperer les cookies, on ne lit PAS les fichiers du navigateur :
   depuis Chrome 127, l'App-Bound Encryption rend `--cookies-from-browser`
   inoperant sous Windows. On demande donc les cookies au navigateur lui-meme
   via le Chrome DevTools Protocol (`Storage.getCookies`), qui nous les rend
   dechiffres. On les ecrit ensuite au format Netscape pour yt-dlp.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .config import settings

# Navigateurs de la famille Chromium acceptes, par ordre de preference.
_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
)

_process: subprocess.Popen | None = None


class BrowserError(RuntimeError):
    pass


def find_browser() -> str | None:
    """Chemin d'un navigateur Chromium installe, ou None."""
    if settings.browser_executable:
        return settings.browser_executable if Path(settings.browser_executable).exists() else None
    for path in _CANDIDATES:
        if Path(path).exists():
            return path
    for name in ("chrome", "chromium", "msedge", "brave"):
        found = shutil.which(name)
        if found:
            return found
    return None


def profile_dir() -> Path:
    """Dossier du profil dedie. Totalement separe du profil personnel."""
    p = settings.data_path / "browser_profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cookies_path() -> Path:
    return settings.data_path / "scraper_cookies.txt"


def is_running() -> bool:
    return _process is not None and _process.poll() is None


async def _cdp_reachable() -> bool:
    url = f"http://127.0.0.1:{settings.browser_debug_port}/json/version"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def bring_to_front() -> bool:
    """Ramene la fenetre du navigateur au premier plan.

    Windows interdit a un processus en arriere-plan de voler le focus : la
    fenetre s'ouvre donc derriere celle depuis laquelle on a clique. On la fait
    remonter via le DevTools Protocol.
    """
    import websockets

    base = f"http://127.0.0.1:{settings.browser_debug_port}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            targets = (await client.get(f"{base}/json/list")).json()
    except (httpx.HTTPError, ValueError):
        return False

    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return False

    try:
        async with websockets.connect(pages[0]["webSocketDebuggerUrl"]) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Page.bringToFront"}))
            await asyncio.wait_for(ws.recv(), timeout=5)
        return True
    except Exception:
        return False


async def launch(start_url: str = "https://www.instagram.com/accounts/login/") -> dict:
    """Ouvre le navigateur isole. Renvoie l'etat de la session."""
    global _process

    if await _cdp_reachable():
        # Deja ouvert : on se contente de le ramener devant, sinon l'utilisateur
        # a l'impression qu'il ne se passe rien.
        await bring_to_front()
        state = await status()
        state["already_running"] = True
        return state

    exe = find_browser()
    if not exe:
        raise BrowserError(
            "Aucun navigateur Chromium trouve (Chrome, Edge ou Brave). "
            "Installe-en un, ou renseigne BROWSER_EXECUTABLE dans .env."
        )

    args = [
        exe,
        f"--user-data-dir={profile_dir()}",
        f"--remote-debugging-port={settings.browser_debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
        # Profil neuf : on evite que le navigateur propose d'importer quoi que
        # ce soit du profil habituel.
        "--disable-sync",
        "--disable-features=ChromeWhatsNewUI",
        start_url,
    ]
    _process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Le port de debug met une seconde ou deux a repondre.
    for _ in range(30):
        if await _cdp_reachable():
            break
        await asyncio.sleep(0.5)
    else:
        raise BrowserError(
            f"Le navigateur a demarre mais le port de debug "
            f"{settings.browser_debug_port} ne repond pas. Un autre navigateur "
            f"utilise peut-etre deja ce port : change BROWSER_DEBUG_PORT."
        )

    await bring_to_front()
    return await status()


# ---------------------------------------------------------------------------
# Capture des cookies via CDP
# ---------------------------------------------------------------------------


async def _cdp_call(method: str, params: dict | None = None) -> dict:
    """Envoie une commande au navigateur via le DevTools Protocol."""
    import websockets

    base = f"http://127.0.0.1:{settings.browser_debug_port}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            info = (await client.get(f"{base}/json/version")).json()
    except httpx.HTTPError as exc:
        raise BrowserError(
            "Le navigateur de scraping n'est pas joignable. Lance-le d'abord."
        ) from exc

    ws_url = info.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BrowserError("Le navigateur n'expose pas d'endpoint DevTools.")

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
            if message.get("id") == 1:
                if "error" in message:
                    raise BrowserError(f"CDP {method} : {message['error']}")
                return message.get("result") or {}


def _to_netscape(cookies: list[dict]) -> str:
    """Convertit les cookies CDP au format Netscape attendu par yt-dlp."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Genere par Workflow IA depuis le navigateur de scraping dedie.",
        "",
    ]
    for c in cookies:
        domain = c.get("domain") or ""
        if not domain:
            continue
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        expires = c.get("expires")
        # -1 (ou absent) = cookie de session : 0 dans le format Netscape.
        expiry = 0 if not expires or expires < 0 else int(expires)
        lines.append(
            "\t".join(
                [
                    domain,
                    include_sub,
                    c.get("path") or "/",
                    "TRUE" if c.get("secure") else "FALSE",
                    str(expiry),
                    c.get("name") or "",
                    c.get("value") or "",
                ]
            )
        )
    return "\n".join(lines) + "\n"


async def capture_cookies() -> dict:
    """Recupere les cookies du navigateur et les ecrit pour yt-dlp.

    On passe par le navigateur (et non par ses fichiers) : c'est ce qui rend la
    manoeuvre insensible a l'App-Bound Encryption de Chrome.
    """
    result = await _cdp_call("Storage.getCookies")
    cookies = result.get("cookies") or []

    path = cookies_path()
    path.write_text(_to_netscape(cookies), encoding="utf-8")

    domains = {c.get("domain", "").lstrip(".") for c in cookies}
    logged_in = {
        "instagram": any(
            c.get("name") == "sessionid" and "instagram" in (c.get("domain") or "")
            for c in cookies
        ),
        "tiktok": any(
            c.get("name") in ("sessionid", "sid_tt") and "tiktok" in (c.get("domain") or "")
            for c in cookies
        ),
    }

    return {
        "cookies": len(cookies),
        "domains": sorted(d for d in domains if d)[:20],
        "logged_in": logged_in,
        "path": str(path),
    }


async def status() -> dict:
    """Etat courant de la session de scraping."""
    path = cookies_path()
    reachable = await _cdp_reachable()
    return {
        "browser_found": find_browser(),
        "running": reachable,
        "profile_dir": str(profile_dir()),
        "cookies_file": str(path) if path.exists() else None,
        "cookies_age_h": (
            round((time.time() - path.stat().st_mtime) / 3600, 1)
            if path.exists()
            else None
        ),
    }


def close() -> None:
    global _process
    if _process and _process.poll() is None:
        _process.terminate()
    _process = None
