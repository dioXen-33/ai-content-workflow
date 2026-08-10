"""Diagnostic de la livraison Telegram.

Rejoue exactement le chemin de code du pipeline, avec une vraie video deja
generee. Aucun appel facture : ni Gemini, ni Kling.

    .venv\\Scripts\\python.exe diag_telegram.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import app  # noqa: F401  (active truststore)
from app.clients import telegram
from app.config import ROOT, settings


def _mask(value: str) -> str:
    if not value:
        return "(vide)"
    return f"{value[:14]}...{value[-4:]}"


async def main() -> int:
    print("=" * 70)
    print("1. CE QUE L'APPLICATION A REELLEMENT CHARGE")
    print("=" * 70)

    env_file = ROOT / ".env"
    print(f"  fichier .env lu     : {env_file}")
    print(f"  existe              : {env_file.exists()}")

    # `repr` volontairement : il rend visibles les espaces, guillemets et
    # caracteres invisibles qui se glissent dans un .env edite a la main.
    print(f"  TELEGRAM_BOT_TOKEN  : {_mask(settings.telegram_bot_token)}")
    print(f"     longueur         : {len(settings.telegram_bot_token)}")
    print(f"  TELEGRAM_CHAT_ID    : {settings.telegram_chat_id!r}")
    print(f"  livraison active    : {telegram.configured()}")

    if not telegram.configured():
        print("\n  -> Un des deux reglages est vide. Le pipeline n'envoie rien.")
        return 1

    # Lecture brute du fichier, pour comparer avec ce que pydantic a charge.
    print("\n  Lignes Telegram brutes dans le fichier :")
    try:
        raw = env_file.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            print("     [!] Le fichier commence par un BOM UTF-8.")
        for line in raw.decode("utf-8", "replace").splitlines():
            if "TELEGRAM" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                print(f"     {key.strip()} = {value!r}")
    except OSError as exc:
        print(f"     lecture impossible : {exc}")

    print("\n" + "=" * 70)
    print("2. LE TOKEN EST-IL ACCEPTE ?")
    print("=" * 70)
    ok, detail = await telegram.check_credentials()
    print(f"  {'OK' if ok else 'ECHEC'} : {detail}")
    if not ok:
        return 1

    print("\n" + "=" * 70)
    print("3. ENVOI D'UNE VRAIE VIDEO DEJA GENEREE")
    print("=" * 70)

    videos = sorted(
        settings.media_path.glob("*/*/output.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not videos:
        print("  Aucune video terminee dans data/media -> rien a envoyer.")
        print("  (Le reste du diagnostic est deja concluant.)")
        return 0

    video = videos[0]
    taille = video.stat().st_size / 1024 / 1024
    print(f"  fichier : {video}")
    print(f"  taille  : {taille:.1f} Mo (plafond Telegram : 50 Mo)")

    try:
        await telegram.send_video(video, caption="[diagnostic] Workflow IA")
    except Exception as exc:  # noqa: BLE001 - on veut le message exact
        print(f"\n  ECHEC : {type(exc).__name__}: {exc}")
        return 1

    print("\n  ENVOI REUSSI -> la video vient de partir sur Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
