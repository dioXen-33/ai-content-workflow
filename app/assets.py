"""Exposition des fichiers locaux en URLs publiques.

Kling telecharge la video de reference et l'image de personnage depuis des URLs
HTTPS joignables depuis Internet. Nos fichiers sont sur disque : ce module fait
le pont.

Deux modes :

- `local`  : l'app sert elle-meme les fichiers sous /public/<token>/... C'est le
             mode naturel sur un VPS, ou le serveur a deja une adresse publique.
- `source` : on renvoie l'URL CDN d'origine issue du scraping. Gratuit et
             instantane, mais ces URLs sont signees et expirent -- utilisable
             seulement si la generation suit immediatement le scraping.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from .config import settings


class AssetHostError(RuntimeError):
    pass


def public_url_for(path: Path, fallback_source_url: str | None = None) -> str:
    """URL publique d'un fichier local, selon le mode configure."""
    mode = settings.asset_host_mode

    if mode == "source":
        if not fallback_source_url:
            raise AssetHostError(
                "ASSET_HOST_MODE=source mais aucune URL d'origine n'est disponible "
                "pour ce fichier."
            )
        # Les schemas internes (`ytdlp://`, `dryrun://`) ne designent pas une URL
        # publique : Kling ne pourrait pas les telecharger.
        if not fallback_source_url.startswith(("http://", "https://")):
            raise AssetHostError(
                "ASSET_HOST_MODE=source est incompatible avec le scraping yt-dlp : "
                "la video n'existe que localement. Bascule sur ASSET_HOST_MODE=local "
                "et renseigne PUBLIC_BASE_URL."
            )
        return fallback_source_url

    if mode != "local":
        raise AssetHostError(f"ASSET_HOST_MODE inconnu : {mode!r}")

    base = settings.resolved_public_base_url()
    if base.startswith("http://127.0.0.1") or base.startswith("http://localhost"):
        raise AssetHostError(
            "PUBLIC_BASE_URL pointe sur localhost : les serveurs de Kling ne "
            "pourront pas telecharger le fichier. Renseigne l'URL publique de ce "
            "serveur dans .env, ou bascule sur ASSET_HOST_MODE=source."
        )

    try:
        rel = path.resolve().relative_to(settings.data_path.resolve())
    except ValueError as exc:
        raise AssetHostError(
            f"{path} est hors du repertoire de donnees, impossible a exposer."
        ) from exc

    token = settings.resolved_asset_token()
    return f"{base}/public/{token}/{rel.as_posix()}"


def as_data_uri(path: Path) -> str:
    """Encode un fichier en data URI.

    L'API Kling accepte l'image de personnage en base64, ce qui evite d'avoir a
    l'exposer publiquement. Reserve aux images : une video de 100 Mo en base64
    ferait exploser la requete.
    """
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def as_base64(path: Path) -> str:
    """Contenu brut en base64, sans prefixe data:."""
    return base64.b64encode(path.read_bytes()).decode("ascii")


def resolve_public_path(relative: str) -> Path:
    """Resout un chemin recu sur la route /public, en refusant les evasions."""
    root = settings.data_path.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise AssetHostError("Chemin hors du repertoire de donnees")
    return target
