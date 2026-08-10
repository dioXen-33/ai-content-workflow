"""Orchestrateur : machine a etats asynchrone sur les videos d'un job.

Principes :

- SQLite est la source de verite. Chaque transition est committee avant de
  passer a la suivante. Un crash ou un `Ctrl+C` ne fait rien repayer.
- Le `kling_task_id` est persiste des la soumission. Si le serveur redemarre
  pendant qu'une generation tourne chez Kling, la relance reprend le polling
  au lieu de resoumettre -- c'est la difference entre 0 et 1,13 USD par video.
- Seules les erreurs `TRANSIENT` sont rejouees. Un refus de securite rejoue a
  l'identique ne ferait que consommer du credit pour le meme refus.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from . import budget, db, media
from .assets import AssetHostError, as_data_uri, public_url_for
from .clients import apify, gemini, kling, telegram, ytdlp
from .config import settings
from .events import bus
from .models import (
    FailureKind,
    GenerationParams,
    JobStatus,
    PipelineError,
    Platform,
    ScrapeParams,
    VideoState,
)

# Echecs transitoires ordinaires (reseau, 500 ponctuel...) : quelques essais
# rapproches suffisent.
MAX_TRANSIENT_RETRIES = 5
RETRY_BACKOFF = (15, 60, 180, 420, 900)

# Saturation annoncee par le fournisseur (503 "high demand"). Elle peut durer des
# heures, et sans modele de repli il n'y a rien d'autre a faire qu'attendre : on
# accorde donc un budget bien plus large. Avec un palier plafonne a 15 min, cela
# represente environ 4 h de patience avant d'abandonner une video.
MAX_OVERLOAD_RETRIES = 20


def _is_overload(exc: PipelineError | None) -> bool:
    """Vrai si le fournisseur a explicitement signale une surcharge."""
    return bool(getattr(exc, "retry_after", None))


def _max_retries(exc: PipelineError | None) -> int:
    return MAX_OVERLOAD_RETRIES if _is_overload(exc) else MAX_TRANSIENT_RETRIES


def _retry_delay(exc: PipelineError | None, attempts: int) -> float:
    """Attente avant un nouvel essai, en secondes."""
    hinted = getattr(exc, "retry_after", None) if exc else None
    if hinted:
        return float(hinted)
    return RETRY_BACKOFF[min(max(attempts - 1, 0), len(RETRY_BACKOFF) - 1)]

# Jobs en cours d'execution, pour permettre l'annulation depuis l'UI.
_cancels: dict[str, asyncio.Event] = {}
_running: dict[str, asyncio.Task] = {}


def is_running(job_id: str) -> bool:
    task = _running.get(job_id)
    return task is not None and not task.done()


def request_cancel(job_id: str) -> bool:
    ev = _cancels.get(job_id)
    if ev is None:
        return False
    ev.set()
    return True


def _cancelled(job_id: str) -> bool:
    ev = _cancels.get(job_id)
    return ev is not None and ev.is_set()


def video_dir(job_id: str, video_id: str) -> Path:
    p = settings.media_path / job_id / video_id
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Etape 1-2 : scraping
# ---------------------------------------------------------------------------


def _parse_account(raw: str) -> tuple[Platform, str] | None:
    """Accepte `@nom`, `nom`, `tiktok:nom`, ou une URL complete.

    Pour Pinterest, la cible n'est pas un compte mais un **tableau** ou un
    **pin** : le lien entier est conserve tel quel, c'est lui qui identifie la
    cible.
    """
    s = raw.strip()
    if not s:
        return None

    low = s.lower()
    if low.startswith(("instagram:", "ig:")):
        return Platform.INSTAGRAM, s.split(":", 1)[1].strip().lstrip("@")
    if low.startswith(("tiktok:", "tt:")):
        return Platform.TIKTOK, s.split(":", 1)[1].strip().lstrip("@")
    if low.startswith(("pinterest:", "pin:")):
        target = s.split(":", 1)[1].strip()
        return (Platform.PINTEREST, target) if target else None

    if "instagram.com" in low:
        handle = low.split("instagram.com/", 1)[1].split("/")[0].split("?")[0]
        return (Platform.INSTAGRAM, handle) if handle else None
    if "tiktok.com" in low:
        handle = low.split("tiktok.com/", 1)[1].split("/")[0].split("?")[0]
        return (Platform.TIKTOK, handle.lstrip("@")) if handle else None
    # `pinterest.fr`, `br.pinterest.com`, `pin.it/xxx`... : on garde l'URL, le
    # client saura distinguer un tableau d'un pin et suivre les liens courts.
    if "pinterest." in low or "pin.it/" in low:
        return Platform.PINTEREST, s

    # Sans indication de plateforme, on scrape les deux.
    return None


def _expand_accounts(accounts: list[str]) -> list[tuple[Platform, str]]:
    out: list[tuple[Platform, str]] = []
    for raw in accounts:
        parsed = _parse_account(raw)
        if parsed:
            out.append(parsed)
        else:
            handle = raw.strip().lstrip("@")
            if handle:
                out.append((Platform.INSTAGRAM, handle))
                out.append((Platform.TIKTOK, handle))
    # Dedup en preservant l'ordre.
    seen: set[tuple[Platform, str]] = set()
    unique = []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


async def _scrape_backend(platform: Platform, handle: str, params: ScrapeParams):
    """Aiguille vers le backend de scraping configure.

    `apify` : API payante, robuste, sans session personnelle.
    `ytdlp` : gratuit, via le navigateur de scraping dedie.
    """
    # Le mode dry-run court-circuite les deux backends : il fabrique des videos
    # localement, sans reseau ni credit.
    if settings.dry_run:
        return await apify.scrape_account(platform, handle, params)
    # Pinterest passe toujours par yt-dlp : aucun acteur Apify n'est configure
    # pour lui, et yt-dlp le gere nativement, gratuitement et sans session.
    if platform == Platform.PINTEREST or settings.scraper_backend == "ytdlp":
        return await ytdlp.scrape_account(platform, handle, params)
    return await apify.scrape_account(platform, handle, params)


async def scrape_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if not job:
        return

    params = ScrapeParams(**job["scrape"])
    targets = _expand_accounts(job["accounts"])

    db.update_job(job_id, status=JobStatus.SCRAPING, error=None)
    bus.emit(job_id, f"Scraping de {len(targets)} cible(s)...")
    bus.progress(job_id, status=JobStatus.SCRAPING)

    total_new = 0
    for platform, handle in targets:
        if _cancelled(job_id):
            bus.emit(job_id, "Scraping interrompu.", level="warn")
            break
        try:
            results = await _scrape_backend(platform, handle, params)
        except PipelineError as exc:
            bus.emit(
                job_id,
                f"@{handle} ({platform}) : {exc.message}",
                level="error",
            )
            continue
        except Exception as exc:
            bus.emit(job_id, f"@{handle} ({platform}) : {exc}", level="error")
            continue

        added = 0
        for item in results:
            if db.upsert_video(job_id, item):
                added += 1
        total_new += added
        bus.emit(
            job_id,
            f"@{handle} ({platform}) : {len(results)} video(s) trouvee(s), "
            f"{added} nouvelle(s).",
        )
        bus.progress(job_id, stats=db.job_stats(job_id))

    db.update_job(job_id, status=JobStatus.REVIEW)
    bus.emit(job_id, f"Scraping termine : {total_new} nouvelle(s) video(s).")
    bus.progress(job_id, status=JobStatus.REVIEW, stats=db.job_stats(job_id))


# ---------------------------------------------------------------------------
# Etapes du pipeline de generation
# ---------------------------------------------------------------------------


# Un meme fichier peut etre demande par l'etape de telechargement et par une
# previsualisation lancee depuis l'UI. Deux ecritures simultanees sur le meme
# `.part` produiraient un fichier corrompu.
_source_locks: dict[str, asyncio.Lock] = {}


async def ensure_local_source(job_id: str, video: dict) -> Path:
    """Garantit la presence du fichier source sur disque, et renvoie son chemin.

    Ne touche pas a l'etat de la video : c'est le socle commun de l'etape de
    telechargement et de la previsualisation avant validation. Rien n'est
    facture ici -- ni Apify, ni Gemini, ni Kling.
    """
    vid = video["id"]
    dest = video_dir(job_id, vid) / "source.mp4"
    source = video["source_url"] or ""

    async with _source_locks.setdefault(vid, asyncio.Lock()):
        # Video uploadee : le fichier est deja a sa place, rien a telecharger.
        if source.startswith(media.UPLOAD_SCHEME):
            if not dest.exists() or dest.stat().st_size == 0:
                raise PipelineError(
                    FailureKind.INVALID_INPUT,
                    "Fichier uploade introuvable sur le disque.",
                )
            return dest

        if not dest.exists() or dest.stat().st_size == 0:
            if source.startswith(media.DRYRUN_SCHEME):
                seed = source.removeprefix(media.DRYRUN_SCHEME)
                await media.generate_test_video(
                    dest, duration=12.0, seed=int(seed) if seed.isdigit() else 0
                )
            elif source.startswith(ytdlp.YTDLP_SCHEME):
                # Les URLs de media Instagram sont signees : on repasse par
                # yt-dlp, qui rejoue la session et reconstruit une URL valide.
                await ytdlp.download(source.removeprefix(ytdlp.YTDLP_SCHEME), dest)
            else:
                await media.download(source, dest, referer=video.get("post_url"))

    return dest


async def _stage_download(job_id: str, video: dict) -> None:
    vid = video["id"]
    dest = await ensure_local_source(job_id, video)
    db.set_state(vid, VideoState.DOWNLOADED, local_path=str(dest))
    bus.state(job_id, vid, VideoState.DOWNLOADED)


async def _stage_frame(job_id: str, video: dict, gen: GenerationParams) -> None:
    """Sonde, filtre, extrait la meilleure frame de debut.

    Tout ce qui est ecarte ici l'est avant le moindre appel payant.
    """
    vid = video["id"]
    d = video_dir(job_id, vid)
    src = Path(video["local_path"])

    info = await media.probe(src)
    frame_path, score = await media.extract_first_frame(src, d)

    params = ScrapeParams(**(db.get_job(job_id) or {}).get("scrape", {}))

    # Les bornes de duree sont un filtre editorial de scraping. Une video
    # importee a ete choisie deliberement : on ne la lui applique pas. Restent
    # les contraintes dures (3 s minimum, ratio, frame exploitable), le plafond
    # de 30 s etant applique au moment de l'envoi a Kling.
    uploaded = video["platform"] == Platform.UPLOAD
    verdict = media.check_eligibility(
        info,
        frame_score=score,
        min_duration_s=0.0 if uploaded else params.min_duration_s,
        max_duration_s=float("inf") if uploaded else params.max_duration_s,
    )

    db.update_video(
        vid,
        duration_s=info.duration_s,
        width=info.width,
        height=info.height,
        frame_path=str(frame_path),
    )

    if not verdict.ok:
        db.set_state(vid, VideoState.SKIPPED, error=verdict.reason, error_kind="filter")
        bus.state(job_id, vid, VideoState.SKIPPED, reason=verdict.reason)
        bus.emit(job_id, f"Ecartee : {verdict.reason}", level="warn", video_id=vid)
        return

    db.set_state(vid, VideoState.FRAMED)
    bus.state(job_id, vid, VideoState.FRAMED, frame_score=round(score, 3))


async def _stage_edit(job_id: str, video: dict, gen: GenerationParams,
                      reference_path: Path) -> None:
    vid = video["id"]
    d = video_dir(job_id, vid)
    out = d / "edited.png"

    cost = 0.0 if settings.dry_run else gen.image_unit_cost()
    budget.guard(job_id, cost)

    spent = await gemini.edit_image(
        frame_path=Path(video["frame_path"]),
        reference_path=reference_path,
        prompt=gen.prompt,
        out_path=out,
        aspect_ratio=gen.aspect_ratio,
        image_size=gen.image_size,
    )
    budget.charge(job_id, vid, spent)

    db.set_state(vid, VideoState.EDITED, edited_path=str(out))
    bus.state(job_id, vid, VideoState.EDITED)
    bus.progress(job_id, budget=budget.summary(job_id))


async def _stage_submit_motion(job_id: str, video: dict, gen: GenerationParams) -> None:
    vid = video["id"]
    d = video_dir(job_id, vid)

    if settings.dry_run:
        db.set_state(vid, VideoState.MOTION_SUBMITTED, kling_task_id="dry-run")
        bus.state(job_id, vid, VideoState.MOTION_SUBMITTED)
        return

    # Motion Control n'a pas de parametre de duree : la sortie fait exactement la
    # longueur de la reference. On envoie donc la video source integrale, tronquee
    # seulement si elle depasse le plafond (reglage utilisateur, borne aux 30 s
    # autorisees par l'API).
    ref_video = await media.trim_for_kling(
        Path(video["local_path"]), d / "reference.mp4",
        max_duration_s=gen.duration_cap(),
    )
    out_duration = (await media.probe(ref_video)).duration_s

    # Le cout suit la duree reellement produite, pas une valeur forfaitaire.
    cost = kling.cost_for(out_duration, gen.kling_mode)
    budget.guard(job_id, cost)

    try:
        video_url = public_url_for(ref_video, fallback_source_url=video.get("source_url"))
    except AssetHostError as exc:
        raise PipelineError(FailureKind.INVALID_INPUT, str(exc)) from exc

    # L'image de personnage part en data URI base64 : l'API accepte URL ou base64
    # pour ce champ, et le data URI evite d'avoir a l'exposer publiquement.
    image_source = as_data_uri(Path(video["edited_path"]))

    task_id = await kling.submit(
        image_source=image_source,
        video_url=video_url,
        prompt="",
        mode=gen.kling_mode,
        keep_audio=gen.keep_audio,
        external_task_id=vid,
    )

    # Facture des la soumission : Kling consomme le credit a ce moment.
    budget.charge(job_id, vid, cost)
    bus.emit(
        job_id,
        f"Kling lance sur {out_duration:.1f} s ({cost:.2f} USD).",
        video_id=vid,
    )
    db.set_state(vid, VideoState.MOTION_SUBMITTED, kling_task_id=task_id)
    bus.state(job_id, vid, VideoState.MOTION_SUBMITTED, task_id=task_id)
    bus.progress(job_id, budget=budget.summary(job_id))


def _telegram_caption(job_id: str, video_id: str) -> str:
    """Legende de la video livree : de quel job et de quelle source elle vient."""
    video = db.get_video(video_id) or {}
    bits = [f"@{video.get('account') or '?'}"]
    if video.get("platform"):
        bits.append(str(video["platform"]))
    if video.get("duration_s"):
        bits.append(f"{float(video['duration_s']):.1f} s")
    cost = float(video.get("cost_usd") or 0)
    if cost:
        bits.append(f"{cost:.2f} USD")

    prefix = "[DRY RUN] " if settings.dry_run else ""
    name = (db.get_job(job_id) or {}).get("name")
    detail = " · ".join(bits)
    return f"{prefix}{name}\n{detail}" if name else f"{prefix}{detail}"


async def _deliver_to_telegram(job_id: str, video_id: str, out: Path) -> None:
    """Livre la video terminee sur Telegram.

    N'echoue jamais. A ce stade la video est produite et payee : une livraison
    ratee doit rester un incident consigne au journal, jamais une video en
    echec ni un job interrompu.
    """
    if not telegram.configured():
        return
    try:
        await telegram.send_video(out, caption=_telegram_caption(job_id, video_id))
    except Exception as exc:  # noqa: BLE001 - la livraison ne casse jamais un job
        bus.emit(
            job_id,
            f"Livraison Telegram echouee : {exc}. La video reste disponible "
            f"dans la galerie.",
            level="warn",
            video_id=video_id,
        )
        return
    bus.emit(job_id, "Video envoyee sur Telegram.", video_id=video_id)


async def _stage_collect(job_id: str, video: dict) -> None:
    vid = video["id"]
    d = video_dir(job_id, vid)
    out = d / "output.mp4"

    if settings.dry_run:
        # On recopie la source : le resultat final existe et est lisible dans l'UI.
        out.write_bytes(Path(video["local_path"]).read_bytes())
        db.set_state(vid, VideoState.DONE, output_path=str(out))
        bus.state(job_id, vid, VideoState.DONE)
        await _deliver_to_telegram(job_id, vid, out)
        return

    def _tick(status: str, waited: float) -> None:
        bus.state(job_id, vid, VideoState.MOTION_SUBMITTED,
                  kling_status=status, waited=int(waited))

    url = await kling.wait_for_result(video["kling_task_id"], on_tick=_tick)
    await media.download(url, out)

    db.set_state(vid, VideoState.DONE, output_path=str(out))
    bus.state(job_id, vid, VideoState.DONE)
    await _deliver_to_telegram(job_id, vid, out)


# ---------------------------------------------------------------------------
# Enchainement par video
# ---------------------------------------------------------------------------

# Etapes pilotees video par video. `EDIT_BATCHED` en est volontairement absent :
# ces videos sont sous la responsabilite du gestionnaire de batchs.
_STAGE_ORDER = [
    VideoState.DISCOVERED,
    VideoState.DOWNLOADED,
    VideoState.FRAMED,
    VideoState.EDITED,
    VideoState.MOTION_SUBMITTED,
]

_NON_TERMINAL = _STAGE_ORDER + [VideoState.EDIT_BATCHED]


async def _process_video(
    job_id: str,
    video_id: str,
    gen: GenerationParams,
    reference_path: Path,
    sems: dict[str, asyncio.Semaphore],
    stop_at: set[str] | None = None,
) -> None:
    while True:
        if _cancelled(job_id):
            return

        video = db.get_video(video_id)
        if not video or video["state"] not in _STAGE_ORDER:
            return  # terminal, ou confie au gestionnaire de batchs

        state = video["state"]
        if stop_at and state in stop_at:
            return
        try:
            if state == VideoState.DISCOVERED:
                async with sems["download"]:
                    await _stage_download(job_id, video)
            elif state == VideoState.DOWNLOADED:
                async with sems["cpu"]:
                    await _stage_frame(job_id, video, gen)
            elif state == VideoState.FRAMED:
                async with sems["gemini"]:
                    await _stage_edit(job_id, video, gen, reference_path)
            elif state == VideoState.EDITED:
                async with sems["kling"]:
                    await _stage_submit_motion(job_id, video, gen)
            elif state == VideoState.MOTION_SUBMITTED:
                async with sems["poll"]:
                    await _stage_collect(job_id, video)

        except budget.BudgetExceeded as exc:
            db.update_job(job_id, status=JobStatus.PAUSED, error=exc.message)
            bus.emit(job_id, exc.message, level="error", video_id=video_id)
            request_cancel(job_id)
            return

        except PipelineError as exc:
            await _handle_failure(job_id, video_id, state, exc)
            if exc.kind != FailureKind.TRANSIENT:
                return
            attempts = db.get_video(video_id)["attempts"]
            if attempts > _max_retries(exc):
                return
            await asyncio.sleep(_retry_delay(exc, attempts))

        except Exception as exc:  # noqa: BLE001
            wrapped = PipelineError(
                FailureKind.UNKNOWN, f"{type(exc).__name__}: {exc}"
            )
            await _handle_failure(job_id, video_id, state, wrapped)
            attempts = db.get_video(video_id)["attempts"]
            if attempts > _max_retries(wrapped):
                return
            await asyncio.sleep(_retry_delay(wrapped, attempts))


async def _handle_failure(
    job_id: str, video_id: str, state: str, exc: PipelineError
) -> None:
    attempts = db.bump_attempts(video_id)
    retriable = exc.kind in (FailureKind.TRANSIENT, FailureKind.UNKNOWN)

    if exc.kind == FailureKind.QUOTA:
        db.update_job(job_id, status=JobStatus.PAUSED, error=exc.message)
        db.update_video(video_id, error=exc.message, error_kind=str(exc.kind))
        bus.emit(job_id, f"Quota epuise : {exc.message}", level="error", video_id=video_id)
        request_cancel(job_id)
        return

    budget_max = _max_retries(exc)
    if retriable and attempts <= budget_max:
        db.update_video(video_id, error=exc.message, error_kind=str(exc.kind))
        delay = _retry_delay(exc, attempts)
        wait = f"{delay / 60:.1f} min" if delay >= 90 else f"{delay:.0f} s"
        motif = "Surcharge" if _is_overload(exc) else "Echec transitoire"
        bus.emit(
            job_id,
            f"{motif} a l'etape {state} (essai {attempts}/{budget_max}, "
            f"nouvel essai dans {wait}) : {exc.message[:200]}",
            level="warn",
            video_id=video_id,
        )
        return

    db.set_state(video_id, VideoState.FAILED, error=exc.message, error_kind=str(exc.kind))
    bus.state(job_id, video_id, VideoState.FAILED, reason=exc.message[:300])
    bus.emit(
        job_id,
        f"Abandon a l'etape {state} ({exc.kind}) : {exc.message[:250]}",
        level="error",
        video_id=video_id,
    )


# ---------------------------------------------------------------------------
# Edition par lots (Batch API Gemini, 50 % du tarif interactif)
#
# Le batch impose un traitement en phases : toutes les videos doivent atteindre
# FRAMED avant la soumission, et Kling ne demarre qu'une fois les images
# recuperees. C'est la contrepartie assumee du demi-tarif.
# ---------------------------------------------------------------------------


async def _finalise_batch(job_id: str, batch: dict, gen: GenerationParams) -> None:
    """Attend un batch soumis, puis distribue ses resultats."""
    name = batch["provider_name"]
    video_ids = batch["video_ids"]

    # Un batch peut durer de quelques minutes a plusieurs heures. Sans trace
    # visible, l'interface semble figee : on ecrit donc une ligne de journal a
    # intervalle regulier, sans pour autant inonder le fil.
    _LOG_EVERY_S = 120
    last_logged = 0.0

    _STATE_FR = {
        "JOB_STATE_PENDING": "en file d'attente",
        "JOB_STATE_RUNNING": "en cours de traitement",
    }

    def _tick(state: str, waited: float) -> None:
        nonlocal last_logged
        bus.progress(
            job_id, batch_state=state, batch_waited_min=int(waited / 60),
            batch_size=len(video_ids),
        )
        if waited - last_logged >= _LOG_EVERY_S:
            last_logged = waited
            label = _STATE_FR.get(state, state)
            bus.emit(
                job_id,
                f"Batch {label} chez Google : {len(video_ids)} image(s), "
                f"{waited / 60:.0f} min ecoulees. Rien a faire, l'attente est "
                f"normale (cible 24 h).",
            )

    try:
        state = await gemini.wait_for_batch(name, on_tick=_tick)
        if state != "JOB_STATE_SUCCEEDED":
            raise PipelineError(
                FailureKind.UNKNOWN, f"Batch termine en {state}."
            )
        outcome = await gemini.collect_batch(name)
    except PipelineError as exc:
        db.update_batch(batch["id"], state="failed", error=exc.message)
        for vid in video_ids:
            if (db.get_video(vid) or {}).get("state") == VideoState.EDIT_BATCHED:
                db.set_state(vid, VideoState.FAILED, error=exc.message,
                             error_kind=str(exc.kind))
                bus.state(job_id, vid, VideoState.FAILED, reason=exc.message[:200])
        bus.emit(job_id, f"Batch en echec : {exc.message[:250]}", level="error")
        return

    unit = gemini.batch_unit_cost(gen.image_size)
    ok = 0
    for vid in video_ids:
        video = db.get_video(vid)
        if not video or video["state"] != VideoState.EDIT_BATCHED:
            continue

        if vid in outcome.images:
            out = video_dir(job_id, vid) / "edited.png"
            out.write_bytes(outcome.images[vid])
            db.set_state(vid, VideoState.EDITED, edited_path=str(out),
                         cost_usd=float(video.get("cost_usd") or 0) + unit)
            bus.state(job_id, vid, VideoState.EDITED)
            ok += 1
        else:
            kind, message = outcome.failures.get(
                vid, (FailureKind.UNKNOWN, "Absente des resultats du batch.")
            )
            db.set_state(vid, VideoState.FAILED, error=message, error_kind=str(kind))
            bus.state(job_id, vid, VideoState.FAILED, reason=message[:200])

    db.update_batch(batch["id"], state="done")
    bus.emit(
        job_id,
        f"Batch termine : {ok}/{len(video_ids)} image(s) recuperee(s).",
    )
    bus.progress(job_id, stats=db.job_stats(job_id), budget=budget.summary(job_id))


async def _run_batch_edit(
    job_id: str, gen: GenerationParams, reference_path: Path
) -> None:
    tasks: list[asyncio.Task] = []

    # 1. Reprise des batchs deja soumis lors d'une execution precedente. Un batch
    #    soumis est deja facture : on recupere ses resultats, on ne resoumet pas.
    for batch in db.open_batches(job_id):
        if batch["provider_name"]:
            bus.emit(
                job_id,
                f"Reprise du batch {batch['provider_name']} "
                f"({len(batch['video_ids'])} image(s)), sans nouvelle facturation.",
            )
            tasks.append(asyncio.create_task(_finalise_batch(job_id, batch, gen)))
        else:
            # Soumission jamais aboutie : les videos repartent en file normale.
            db.update_batch(batch["id"], state="failed", error="Soumission interrompue.")
            for vid in batch["video_ids"]:
                if (db.get_video(vid) or {}).get("state") == VideoState.EDIT_BATCHED:
                    db.set_state(vid, VideoState.FRAMED)

    # 2. Soumission du reste, par tranches.
    pending = db.list_videos(job_id, states=[VideoState.FRAMED], selected_only=True)
    unit = gemini.batch_unit_cost(gen.image_size)
    size = max(settings.gemini_batch_chunk_size, 1)

    for start in range(0, len(pending), size):
        if _cancelled(job_id):
            break
        chunk = pending[start:start + size]
        ids = [v["id"] for v in chunk]
        cost = unit * len(chunk)

        try:
            budget.guard(job_id, cost)
        except budget.BudgetExceeded as exc:
            db.update_job(job_id, status=JobStatus.PAUSED, error=exc.message)
            bus.emit(job_id, exc.message, level="error")
            request_cancel(job_id)
            break

        batch_id = db.create_batch(job_id, ids, cost)
        for vid in ids:
            db.set_state(vid, VideoState.EDIT_BATCHED)
            bus.state(job_id, vid, VideoState.EDIT_BATCHED)

        try:
            name = await gemini.submit_batch(
                items=[
                    gemini.BatchItem(key=v["id"], frame_path=Path(v["frame_path"]))
                    for v in chunk
                ],
                reference_path=reference_path,
                prompt=gen.prompt,
                workdir=settings.media_path / job_id / "_batches",
                aspect_ratio=gen.aspect_ratio,
                image_size=gen.image_size,
            )
        except PipelineError as exc:
            db.update_batch(batch_id, state="failed", error=exc.message)
            for vid in ids:
                db.set_state(vid, VideoState.FRAMED)  # rien n'a ete facture
            bus.emit(job_id, f"Soumission du batch echouee : {exc.message[:250]}",
                     level="error")
            continue

        db.update_batch(batch_id, provider_name=name, state="submitted")
        budget.charge(job_id, None, cost)
        bus.emit(
            job_id,
            f"Batch soumis : {len(chunk)} image(s) a {unit:.3f} USD "
            f"(moitie du tarif interactif). Traitement asynchrone, cible 24 h.",
        )
        bus.progress(job_id, budget=budget.summary(job_id))

        tasks.append(
            asyncio.create_task(
                _finalise_batch(job_id, db.get_batch(batch_id), gen)
            )
        )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# Job complet
# ---------------------------------------------------------------------------


async def run_job(job_id: str, gen: GenerationParams) -> None:
    job = db.get_job(job_id)
    if not job:
        return

    reference = db.get_reference(gen.reference_image_id)
    if not reference:
        db.update_job(job_id, status=JobStatus.FAILED, error="Image de reference introuvable.")
        bus.emit(job_id, "Image de reference introuvable.", level="error")
        return
    reference_path = Path(reference["path"])

    _cancels[job_id] = asyncio.Event()
    db.update_job(job_id, status=JobStatus.RUNNING, generation=gen.model_dump(), error=None)

    sems = {
        "download": asyncio.Semaphore(settings.concurrency_download),
        "cpu": asyncio.Semaphore(max(2, settings.concurrency_download)),
        "gemini": asyncio.Semaphore(settings.concurrency_gemini),
        "kling": asyncio.Semaphore(settings.concurrency_kling),
        # Le polling est peu couteux : on ne bride pas autant que la soumission.
        "poll": asyncio.Semaphore(max(settings.concurrency_kling * 4, 8)),
    }

    # Reprise : on ramasse tout ce qui n'est pas terminal, y compris les taches
    # Kling et les batchs Gemini deja soumis lors d'une execution precedente.
    pending = db.list_videos(job_id, states=_NON_TERMINAL, selected_only=True)
    resumed = [v for v in pending if v["state"] == VideoState.MOTION_SUBMITTED]
    if resumed:
        bus.emit(
            job_id,
            f"Reprise : {len(resumed)} tache(s) Kling deja soumise(s), polling repris "
            f"sans nouvelle facturation.",
        )

    bus.emit(job_id, f"Lancement sur {len(pending)} video(s).")
    bus.progress(job_id, status=JobStatus.RUNNING, stats=db.job_stats(job_id))

    if gen.gemini_batch and not settings.dry_run:
        # Le Batch API impose de traiter en phases.
        bus.emit(job_id, "Mode batch actif : Nano Banana Pro a 50 % du tarif.")

        # Phase 1 : amener toutes les videos jusqu'a la frame extraite.
        await asyncio.gather(
            *(
                _process_video(job_id, v["id"], gen, reference_path, sems,
                               stop_at={VideoState.FRAMED})
                for v in pending
            ),
            return_exceptions=True,
        )

        # Phase 2 : edition groupee.
        if not _cancelled(job_id):
            await _run_batch_edit(job_id, gen, reference_path)

        # Phase 3 : Kling, video par video comme d'habitude.
        remaining = db.list_videos(job_id, states=_STAGE_ORDER, selected_only=True)
        await asyncio.gather(
            *(
                _process_video(job_id, v["id"], gen, reference_path, sems)
                for v in remaining
            ),
            return_exceptions=True,
        )
    else:
        await asyncio.gather(
            *(
                _process_video(job_id, v["id"], gen, reference_path, sems)
                for v in pending
            ),
            return_exceptions=True,
        )

    stats = db.job_stats(job_id)
    current = db.get_job(job_id) or {}
    if current.get("status") == JobStatus.PAUSED:
        final = JobStatus.PAUSED
    elif _cancelled(job_id):
        final = JobStatus.PAUSED
    else:
        final = JobStatus.COMPLETED

    db.update_job(job_id, status=final)
    _cancels.pop(job_id, None)

    bus.emit(
        job_id,
        f"Termine. {stats.get('done', 0)} reussie(s), {stats.get('failed', 0)} en echec, "
        f"{stats.get('skipped', 0)} ecartee(s). Depense : "
        f"{budget.summary(job_id)['spent_usd']:.2f} USD.",
    )
    bus.progress(job_id, status=final, stats=stats, budget=budget.summary(job_id))


# ---------------------------------------------------------------------------
# Lancement en tache de fond
# ---------------------------------------------------------------------------


def launch(job_id: str, coro) -> None:
    if is_running(job_id):
        # La coroutine a ete construite par l'appelant mais ne sera pas planifiee :
        # on la ferme pour eviter un "coroutine was never awaited".
        coro.close()
        raise PipelineError(FailureKind.INVALID_INPUT, "Ce job est deja en cours.")
    _cancels.setdefault(job_id, asyncio.Event()).clear()
    task = asyncio.create_task(coro)
    _running[job_id] = task

    def _done(t: asyncio.Task) -> None:
        _running.pop(job_id, None)
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            db.update_job(job_id, status=JobStatus.FAILED, error=str(exc))
            bus.emit(job_id, f"Job interrompu : {exc}", level="error")

    task.add_done_callback(_done)
