"""Point d'entree : lance le serveur web.

    python run.py
"""

from __future__ import annotations

import sys

import uvicorn

from app.config import settings
from app.media import ffmpeg_available


def main() -> int:
    if not ffmpeg_available():
        print(
            "ERREUR : ffmpeg et ffprobe sont introuvables dans le PATH.\n"
            "         Ils sont indispensables a l'extraction des frames.\n"
            "         Windows : winget install Gyan.FFmpeg\n"
            "         Debian  : sudo apt install ffmpeg",
            file=sys.stderr,
        )
        return 1

    missing = settings.missing_keys()
    if missing and not settings.dry_run:
        print(f"Attention : cles absentes du .env -> {', '.join(missing)}")
        print("            L'interface demarre quand meme ; utilise DRY_RUN=true")
        print("            pour valider le pipeline sans depenser de credit.\n")

    for warning in settings.warnings():
        print(f"Attention : {warning}\n")

    url = f"http://{settings.host}:{settings.port}"
    print(f"Workflow IA demarre sur {url}")
    if settings.dry_run:
        print("Mode DRY RUN : aucun appel API facture ne sera emis.")

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
