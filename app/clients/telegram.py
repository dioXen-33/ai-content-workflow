"""Livraison des videos terminees sur Telegram, via la Bot API.

Le pipeline appelle `send_video` juste apres le passage d'une video en DONE.

Deux partis pris :

- **La livraison ne fait jamais echouer une video.** A ce stade elle est
  produite et payee ; un probleme reseau cote Telegram ne doit pas la faire
  retomber en echec. C'est l'appelant qui absorbe les erreurs, ce module se
  contente de les decrire.
- **Les envois sont serialises.** La Bot API limite le debit par conversation,
  et plusieurs videos terminant simultanement se feraient jeter en 429.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from ..config import settings

API = "https://api.telegram.org"

# Plafond d'upload impose aux bots par la Bot API.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# Telegram tronque les legendes a 1024 caracteres ; on garde de la marge.
MAX_CAPTION = 1000

MAX_ATTEMPTS = 3

# Une seule video a la fois sur le fil, quelle que soit la concurrence du
# pipeline.
_send_lock = asyncio.Lock()


class TelegramError(RuntimeError):
    pass


def configured() -> bool:
    """Vrai si la livraison est activee (les deux reglages sont renseignes)."""
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _url(method: str) -> str:
    return f"{API}/bot{settings.telegram_bot_token}/{method}"


async def send_video(path: Path, caption: str = "") -> None:
    """Envoie une video. Leve `TelegramError` si elle n'a pas pu etre remise."""
    if not configured():
        raise TelegramError(
            "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID absent du .env."
        )
    if not path.exists() or path.stat().st_size == 0:
        raise TelegramError(f"Fichier absent ou vide : {path.name}")

    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise TelegramError(
            f"{path.name} pese {size / 1024 / 1024:.0f} Mo : au-dela des "
            f"{MAX_UPLOAD_BYTES // 1024 // 1024} Mo qu'un bot Telegram peut "
            f"televerser. La video reste disponible dans la galerie."
        )

    fields = {"chat_id": str(settings.telegram_chat_id), "supports_streaming": "true"}
    if caption:
        fields["caption"] = caption[:MAX_CAPTION]

    async with _send_lock:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(300.0, connect=20.0)
                ) as client:
                    with path.open("rb") as fh:
                        resp = await client.post(
                            _url("sendVideo"),
                            data=fields,
                            files={"video": (path.name, fh, "video/mp4")},
                        )
            except httpx.HTTPError as exc:
                if last:
                    raise TelegramError(f"Telegram injoignable : {exc}") from exc
                await asyncio.sleep(5 * attempt)
                continue

            try:
                body = resp.json()
            except ValueError:
                body = {}

            if body.get("ok"):
                return

            detail = body.get("description") or resp.text[:200] or "sans detail"

            # 429 : Telegram indique lui-meme le delai a respecter.
            if resp.status_code == 429 and not last:
                wait = (body.get("parameters") or {}).get("retry_after") or 5
                await asyncio.sleep(float(wait))
                continue

            if resp.status_code >= 500 and not last:
                await asyncio.sleep(5 * attempt)
                continue

            raise TelegramError(f"refus de Telegram ({resp.status_code}) : {detail}")

    raise TelegramError("envoi abandonne apres plusieurs tentatives.")


async def check_credentials() -> tuple[bool, str]:
    """Verifie le token sans rien envoyer. Utilise par les scripts de controle."""
    if not configured():
        return False, "TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            resp = await client.get(_url("getMe"))
        body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"Telegram injoignable : {exc}"

    if not body.get("ok"):
        return False, f"Token refuse : {body.get('description', 'sans detail')}"
    username = (body.get("result") or {}).get("username") or "?"
    return True, f"Bot @{username} valide, livraison vers {settings.telegram_chat_id}"
