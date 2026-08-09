"""Test bout-en-bout du pipeline en mode dry-run.

Ne consomme aucun credit : les clients Apify, Gemini et Kling renvoient des
resultats factices. Verifie que la machine a etats, ffmpeg, l'extraction de
frame et la persistance fonctionnent.

    python smoke_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys

os.environ["DRY_RUN"] = "true"

from app import db, pipeline  # noqa: E402
from app.config import settings  # noqa: E402
from app.media import ffmpeg_available  # noqa: E402
from app.models import GenerationParams, ScrapeParams, VideoState  # noqa: E402


async def main() -> int:
    if not ffmpeg_available():
        print("ffmpeg/ffprobe introuvables -> test impossible.")
        return 1
    if not settings.dry_run:
        print("DRY_RUN n'est pas actif -> abandon pour ne rien facturer.")
        return 1

    db.init()

    scrape = ScrapeParams(
        max_videos_per_account=3,
        min_duration_s=3,
        max_duration_s=30,
    )
    job_id = db.create_job("smoke-test", ["demo_account"], scrape.model_dump(), 5.0)
    print(f"Job {job_id} cree.")

    await pipeline.scrape_job(job_id)
    videos = db.list_videos(job_id)
    print(f"Scraping : {len(videos)} video(s).")
    if not videos:
        print("Aucune video renvoyee par le mode dry-run.")
        return 1

    db.set_selection(job_id, [v["id"] for v in videos])

    # Image de reference factice : on reutilise une image generee par PIL.
    from PIL import Image

    ref_path = settings.refs_path / "smoke-ref.png"
    Image.new("RGB", (512, 512), (40, 90, 200)).save(ref_path)
    ref_id = db.add_reference("smoke-ref.png", ref_path)

    gen = GenerationParams(
        prompt="[dry-run] transformation de test",
        reference_image_id=ref_id,
        max_output_duration_s=30,
        kling_mode="std",
    )

    await pipeline.run_job(job_id, gen)

    stats = db.job_stats(job_id)
    print(f"\nEtats finaux : {stats}")

    done = db.list_videos(job_id, states=[VideoState.DONE])
    failed = db.list_videos(job_id, states=[VideoState.FAILED])
    skipped = db.list_videos(job_id, states=[VideoState.SKIPPED])

    for v in failed:
        print(f"  ECHEC   @{v['account']} : {v['error']}")
    for v in skipped:
        print(f"  ECARTEE @{v['account']} : {v['error']}")
    for v in done:
        print(f"  OK      @{v['account']} -> {v['output_path']}")

    ok = len(done) > 0
    print("\n" + ("PIPELINE OK" if ok else "PIPELINE KO : aucune video terminee"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
