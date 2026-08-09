"""Cree des jobs de demonstration, sans consommer un seul credit.

Force le mode dry-run : les vidéos sont fabriquees localement avec ffmpeg, les
appels Apify / Gemini / Kling sont factices. Les jobs sont laisses dans des
etats varies pour illustrer chaque ecran de l'interface.

    python seed_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ["DRY_RUN"] = "true"

from PIL import Image  # noqa: E402

from app import db, pipeline  # noqa: E402
from app.config import settings  # noqa: E402
from app.media import ffmpeg_available  # noqa: E402
from app.models import GenerationParams, ScrapeParams  # noqa: E402


def _reference() -> str:
    """Une image de reference factice, partagee par les jobs de demo."""
    path = settings.refs_path / "demo-personnage.png"
    if not path.exists():
        img = Image.new("RGB", (768, 768), (34, 96, 168))
        img.save(path)
    existing = [r for r in db.list_references() if r["filename"] == "demo-personnage.png"]
    if existing:
        return existing[0]["id"]
    return db.add_reference("demo-personnage.png", path)


async def _make_completed(ref_id: str) -> None:
    """Job entierement traite -> illustre l'ecran Resultats."""
    scrape = ScrapeParams(max_videos_per_account=4)
    job_id = db.create_job(
        "Campagne Sport — Août", ["athlete_motivation", "gym_daily"],
        scrape.model_dump(), 50.0,
    )
    await pipeline.scrape_job(job_id)
    db.set_selection(job_id, [v["id"] for v in db.list_videos(job_id)])
    gen = GenerationParams(
        prompt="Remplace la personne par le personnage de reference, "
               "meme tenue de sport, meme energie.",
        reference_image_id=ref_id,
        max_output_duration_s=30,
        kling_mode="pro",
    )
    await pipeline.run_job(job_id, gen)
    print(f"  [Resultats]  {db.get_job(job_id)['name']} -> {db.job_stats(job_id)}")


async def _make_review(ref_id: str) -> None:
    """Job scrape mais pas encore valide -> illustre l'ecran Validation."""
    scrape = ScrapeParams(max_videos_per_account=4)
    job_id = db.create_job(
        "Influenceurs Fitness", ["fitgirl_pro", "coach_thomas", "workout_home"],
        scrape.model_dump(), 30.0,
    )
    await pipeline.scrape_job(job_id)   # laisse le job en statut REVIEW
    print(f"  [Validation] {db.get_job(job_id)['name']} -> "
          f"{len(db.list_videos(job_id))} video(s) a valider")


async def _make_partial(ref_id: str) -> None:
    """Job traite puis mis en pause -> illustre la reprise et le suivi budget."""
    scrape = ScrapeParams(max_videos_per_account=4)
    job_id = db.create_job(
        "Cuisine Rapide", ["recettes_express", "batchcooking"],
        scrape.model_dump(), 20.0,
    )
    await pipeline.scrape_job(job_id)
    videos = db.list_videos(job_id)
    db.set_selection(job_id, [v["id"] for v in videos])

    gen = GenerationParams(
        prompt="Remplace le cuisinier par le personnage de reference.",
        reference_image_id=ref_id, max_output_duration_s=15, kling_mode="std",
    )
    # Le pipeline gere proprement le schema dryrun:// et fabrique les videos.
    await pipeline.run_job(job_id, gen)

    # On remet une partie des videos en attente et on marque le job "en pause"
    # pour illustrer le bouton Reprendre et le suivi budget partiel.
    from app.models import VideoState

    for v in db.list_videos(job_id, states=[VideoState.DONE])[2:]:
        db.set_state(v["id"], VideoState.DISCOVERED, output_path=None)
    db.update_job(job_id, status="paused")
    print(f"  [En pause]   {db.get_job(job_id)['name']} -> "
          f"{db.job_stats(job_id)} (reprise possible)")


async def main() -> int:
    if not ffmpeg_available():
        print("ffmpeg introuvable -> impossible de generer les videos de demo.")
        return 1
    if not settings.dry_run:
        print("DRY_RUN inactif -> abandon.")
        return 1

    db.init()

    # Idempotent : on repart des demo existantes pour eviter les doublons.
    demo_names = {"Campagne Sport — Août", "Influenceurs Fitness", "Cuisine Rapide"}
    for job in db.list_jobs():
        if job["name"] in demo_names:
            db.delete_job(job["id"])

    ref_id = _reference()
    print("Creation des jobs de demonstration (aucun credit consomme) :\n")

    await _make_completed(ref_id)
    await _make_review(ref_id)
    await _make_partial(ref_id)

    print("\nTermine. Lance le serveur puis ouvre http://127.0.0.1:8000")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
