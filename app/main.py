"""API FastAPI + service de l'interface web."""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from . import browser, budget, db, media, pipeline
from .assets import AssetHostError, resolve_public_path
from .clients import gemini, kling, ytdlp
from .config import settings
from .events import bus, sse
from .models import (
    CreateJobRequest,
    FailureKind,
    GenerationParams,
    JobStatus,
    PipelineError,
    Platform,
    Preferences,
    StartRunRequest,
    VideoState,
    estimate_video_cost,
)


def current_preferences() -> Preferences:
    """Preferences enregistrees, completees par les valeurs du .env."""
    stored = db.get_preferences()
    stored.setdefault("max_spend_usd", settings.max_spend_usd)
    return Preferences(**stored)

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    yield


app = FastAPI(title="Workflow IA", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Sert l'interface, avec un cache-buster sur les fichiers statiques.

    Sans cela, le navigateur peut garder un app.js perime alors que le HTML est
    a jour : les boutons s'affichent mais aucun gestionnaire ne leur est
    attache, et ils restent inertes sans message d'erreur.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for asset in ("app.js", "style.css"):
        try:
            version = int((STATIC / asset).stat().st_mtime)
        except OSError:
            version = 0
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={version}")
    return HTMLResponse(
        html, headers={"Cache-Control": "no-store, must-revalidate"}
    )


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    return {
        "dry_run": settings.dry_run,
        "ffmpeg": media.ffmpeg_available(),
        "missing_keys": settings.missing_keys(),
        "warnings": settings.warnings(),
        "asset_host_mode": settings.asset_host_mode,
        "public_base_url": settings.resolved_public_base_url(),
        "kling_endpoint": settings.kling_base_url.rstrip("/")
        + settings.kling_motion_control_path,
        "gemini_model": settings.gemini_image_model,
        "max_spend_usd": settings.max_spend_usd,
    }


@app.post("/api/diagnostics")
async def diagnostics() -> dict:
    """Teste reellement les credentials. Aucun appel facture."""
    checks: list[dict] = [
        {
            "name": "ffmpeg / ffprobe",
            "ok": media.ffmpeg_available(),
            "detail": "Disponibles" if media.ffmpeg_available()
            else "Introuvables dans le PATH. Installe ffmpeg.",
        },
    ]

    # Le controle du scraping depend du backend choisi.
    if settings.scraper_backend == "ytdlp":
        state = await browser.status()
        has_cookies = bool(state["cookies_file"])
        checks.append(
            {
                "name": "Scraping (yt-dlp)",
                "ok": ytdlp.available() and has_cookies,
                "detail": (
                    f"yt-dlp pret, session capturee il y a "
                    f"{state['cookies_age_h']} h"
                    if ytdlp.available() and has_cookies
                    else (
                        "yt-dlp absent : pip install yt-dlp"
                        if not ytdlp.available()
                        else "Aucune session capturee. Parametres > Navigateur de "
                             "scraping : connecte le compte dedie puis capture."
                    )
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "Scraping (Apify)",
                "ok": bool(settings.apify_token),
                "detail": "Token present"
                if settings.apify_token
                else "APIFY_TOKEN manquant",
            }
        )

    ok, detail = await gemini.check_credentials()
    checks.append({"name": "Nano Banana Pro (Gemini)", "ok": ok, "detail": detail})

    ok, detail = await kling.check_credentials()
    checks.append({"name": "Kling Motion Control", "ok": ok, "detail": detail})

    host_ok, host_detail = True, "OK"
    if settings.asset_host_mode == "local":
        base = settings.resolved_public_base_url()
        if base.startswith(("http://127.0.0.1", "http://localhost")):
            host_ok = False
            host_detail = (
                "PUBLIC_BASE_URL pointe sur localhost : Kling ne pourra pas "
                "telecharger les videos. Renseigne l'URL publique du serveur."
            )
        else:
            host_detail = f"Les fichiers seront servis depuis {base}/public/..."
    else:
        host_detail = "Mode `source` : les URLs CDN d'origine seront transmises a Kling."
    checks.append({"name": "Exposition des fichiers", "ok": host_ok, "detail": host_detail})

    return {"checks": checks, "all_ok": all(c["ok"] for c in checks)}


# ---------------------------------------------------------------------------
# Images de reference
# ---------------------------------------------------------------------------


@app.post("/api/references")
async def upload_reference(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "ref.png").suffix.lower() or ".png"
    if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(400, "Formats acceptes : png, jpg, jpeg, webp.")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image trop lourde (limite Kling : 10 Mo).")

    ref_id = db.add_reference(file.filename or f"ref{suffix}", Path("pending"))
    dest = settings.refs_path / f"{ref_id}{suffix}"
    dest.write_bytes(data)
    db.set_reference_path(ref_id, dest)

    return {"id": ref_id, "filename": file.filename, "url": f"/api/references/{ref_id}/file"}


@app.get("/api/references")
async def list_references() -> list[dict]:
    return [
        {"id": r["id"], "filename": r["filename"], "url": f"/api/references/{r['id']}/file"}
        for r in db.list_references()
    ]


@app.get("/api/references/{ref_id}/file")
async def reference_file(ref_id: str) -> FileResponse:
    ref = db.get_reference(ref_id)
    if not ref or not Path(ref["path"]).exists():
        raise HTTPException(404, "Image introuvable")
    return FileResponse(ref["path"])


@app.delete("/api/references/{ref_id}")
async def reference_delete(ref_id: str) -> dict:
    ref = db.get_reference(ref_id)
    if not ref:
        raise HTTPException(404, "Image introuvable")

    # Une generation en cours relit l'image a chaque appel : la supprimer sous
    # ses pieds ferait echouer les videos restantes.
    for job in db.list_jobs():
        if job["status"] not in (JobStatus.RUNNING, JobStatus.SCRAPING):
            continue
        full = db.get_job(job["id"]) or {}
        gen = full.get("generation") or {}
        if gen.get("reference_image_id") == ref_id:
            raise HTTPException(
                409,
                f"Image utilisee par le job « {job['name']} » en cours. "
                f"Mets-le en pause ou attends la fin avant de la supprimer.",
            )

    Path(ref["path"]).unlink(missing_ok=True)
    db.delete_reference(ref_id)

    prefs = current_preferences()
    if prefs.reference_image_id == ref_id:
        prefs.reference_image_id = ""
        db.set_preferences(prefs.model_dump())
    return {"ok": True}


# ---------------------------------------------------------------------------
# Navigateur de scraping dedie
#
# Profil totalement isole : aucun cookie ni compte du navigateur personnel n'y
# transite. L'utilisateur y connecte un compte dedie au scraping.
# ---------------------------------------------------------------------------


@app.get("/api/browser/status")
async def browser_status() -> dict:
    state = await browser.status()
    state["backend"] = settings.scraper_backend
    state["ytdlp"] = ytdlp.available()
    return state


@app.post("/api/browser/launch")
async def browser_launch(payload: dict | None = None) -> dict:
    target = (payload or {}).get("url") or "https://www.instagram.com/accounts/login/"
    try:
        return await browser.launch(target)
    except browser.BrowserError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/browser/capture")
async def browser_capture() -> dict:
    try:
        return await browser.capture_cookies()
    except browser.BrowserError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/browser/close")
async def browser_close() -> dict:
    browser.close()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


@app.get("/api/preferences")
async def preferences_get() -> dict:
    return current_preferences().model_dump()


@app.put("/api/preferences")
async def preferences_put(prefs: Preferences) -> dict:
    db.set_preferences(prefs.model_dump())
    return prefs.model_dump()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@app.get("/api/jobs")
async def jobs_list() -> list[dict]:
    return db.list_jobs()


@app.post("/api/jobs")
async def job_create(req: CreateJobRequest) -> dict:
    # Le plafond budgetaire vient des preferences : il n'est pas ressaisi a
    # chaque job.
    job_id = db.create_job(
        req.name, req.accounts, req.scrape.model_dump(),
        current_preferences().max_spend_usd,
    )
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_get(job_id: str) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    job["stats"] = db.job_stats(job_id)
    job["budget"] = budget.summary(job_id)
    job["running"] = pipeline.is_running(job_id)
    return job


@app.delete("/api/jobs/{job_id}")
async def job_delete(job_id: str) -> dict:
    db.delete_job(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/scrape")
async def job_scrape(job_id: str) -> dict:
    if not db.get_job(job_id):
        raise HTTPException(404, "Job introuvable")
    try:
        pipeline.launch(job_id, pipeline.scrape_job(job_id))
    except PipelineError as exc:
        raise HTTPException(409, exc.message) from exc
    return {"ok": True}


_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 Mo par fichier
_UPLOAD_CHUNK = 1 << 20


class _UploadTooLarge(Exception):
    pass


async def _stream_to_disk(upload: UploadFile, dest: Path) -> int:
    """Ecrit un fichier recu par blocs. Renvoie sa taille.

    Un envoi de 500 Mo ne doit pas transiter integralement par la memoire :
    plusieurs fichiers simultanes suffiraient sinon a faire tomber le serveur.
    """
    size = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        while chunk := await upload.read(_UPLOAD_CHUNK):
            size += len(chunk)
            if size > _MAX_UPLOAD_BYTES:
                raise _UploadTooLarge
            fh.write(chunk)
    return size


@app.post("/api/jobs/{job_id}/upload")
async def job_upload(job_id: str, files: list[UploadFile] = File(...)) -> dict:
    """Ajoute des videos fournies directement, sans scraping.

    Chaque fichier devient une video en etat DISCOVERED avec son fichier deja
    sur disque : le pipeline l'enchaine ensuite comme une video scrapee, a
    partir de l'extraction de frame.
    """
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")

    added: list[dict] = []
    errors: list[str] = []
    staging = settings.media_path / job_id / "_uploads"

    for upload in files:
        name = upload.filename or "video.mp4"
        suffix = Path(name).suffix.lower()
        if suffix not in _VIDEO_SUFFIXES:
            errors.append(f"{name} : format non supporte ({suffix or 'inconnu'})")
            continue

        # Zone de transit, sous un nom genere : le nom fourni par le client
        # n'atteint jamais le disque. La ligne en base n'est creee qu'une fois
        # le fichier complet, pour ne jamais laisser d'entree orpheline.
        tmp = staging / f"{uuid.uuid4().hex}{suffix}"
        try:
            size = await _stream_to_disk(upload, tmp)
        except _UploadTooLarge:
            tmp.unlink(missing_ok=True)
            errors.append(f"{name} : trop lourd (> 500 Mo)")
            continue
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            errors.append(f"{name} : ecriture impossible ({exc})")
            continue
        if size == 0:
            tmp.unlink(missing_ok=True)
            errors.append(f"{name} : fichier vide")
            continue

        vid = db.upsert_video(
            job_id,
            {
                "platform": str(Platform.UPLOAD),
                "account": Path(name).stem[:60] or "upload",
                "external_id": f"upload-{uuid.uuid4().hex[:10]}",
                "post_url": None,
                "source_url": f"{media.UPLOAD_SCHEME}{name}",
                "caption": name,
                "thumbnail_url": None,
            },
        )
        if not vid:
            tmp.unlink(missing_ok=True)
            errors.append(f"{name} : doublon ignore")
            continue

        d = pipeline.video_dir(job_id, vid)
        dest = d / "source.mp4"
        # On normalise en .mp4 quel que soit le conteneur d'origine : ffmpeg lit
        # tout, et la preparation pour Kling re-encodera si besoin.
        try:
            if suffix == ".mp4":
                tmp.replace(dest)
            else:
                await media.remux_to_mp4(tmp, dest)
        except (OSError, RuntimeError) as exc:
            db.set_state(
                vid, VideoState.FAILED,
                error=f"Conversion en mp4 impossible : {exc}",
                error_kind=str(FailureKind.INVALID_INPUT),
            )
            errors.append(f"{name} : conversion en mp4 impossible")
            continue
        finally:
            tmp.unlink(missing_ok=True)

        # Sonde + frame d'apercu, pour afficher une vignette des la validation.
        # Un fichier illisible est ecarte tout de suite : le laisser filer dans
        # le pipeline lui ferait consommer cinq essais et un quart d'heure de
        # backoff pour aboutir au meme constat.
        try:
            info = await media.probe(dest)
            frame_path, _ = await media.extract_first_frame(dest, d)
        except Exception as exc:  # noqa: BLE001 - fichier corrompu ou tronque
            db.set_state(
                vid, VideoState.FAILED, local_path=str(dest),
                error=f"Fichier illisible : {exc}",
                error_kind=str(FailureKind.INVALID_INPUT),
            )
            errors.append(f"{name} : fichier illisible")
            continue

        db.update_video(
            vid,
            local_path=str(dest),
            duration_s=info.duration_s,
            width=info.width,
            height=info.height,
            frame_path=str(frame_path),
        )
        added.append({"id": vid, "filename": name, "duration_s": info.duration_s})

    if added:
        bus.emit(job_id, f"{len(added)} video(s) importee(s).")
        # Les videos importees passent par la meme validation que les scrapees.
        # On ne redescend pas un job en cours : ses nouvelles videos, deja
        # selectionnees, seront prises au prochain passage.
        if job["status"] not in (JobStatus.RUNNING, JobStatus.SCRAPING):
            db.update_job(job_id, status=JobStatus.REVIEW)
        bus.progress(job_id, status=db.get_job(job_id)["status"],
                     stats=db.job_stats(job_id))

    return {"added": len(added), "videos": added, "errors": errors}


@app.get("/api/jobs/{job_id}/videos")
async def job_videos(job_id: str) -> list[dict]:
    videos = db.list_videos(job_id)
    for v in videos:
        v["has_frame"] = bool(v.get("frame_path"))
        v["has_edited"] = bool(v.get("edited_path"))
        v["has_output"] = bool(v.get("output_path"))
    return videos


@app.post("/api/jobs/{job_id}/selection")
async def job_selection(job_id: str, payload: dict) -> dict:
    ids = payload.get("video_ids") or []
    db.set_selection(job_id, ids)
    return {"ok": True, "selected": len(ids), "stats": db.job_stats(job_id)}


@app.post("/api/jobs/{job_id}/estimate")
async def job_estimate(job_id: str, gen: GenerationParams) -> dict:
    """Estimation basee sur la duree reelle de chaque video selectionnee.

    Kling facture a la seconde produite, et la sortie fait la longueur de la
    source : le cout depend donc directement de la duree de chaque video, pas
    d'un forfait.
    """
    pending = db.list_videos(
        job_id,
        states=[
            VideoState.DISCOVERED,
            VideoState.DOWNLOADED,
            VideoState.FRAMED,
            VideoState.EDITED,
        ],
        selected_only=True,
    )

    cap = gen.duration_cap()
    total = 0.0
    durations: list[float] = []
    unknown = 0

    for v in pending:
        duration = v.get("duration_s")
        if duration:
            duration = min(float(duration), cap)
        else:
            # Duree inconnue avant telechargement : on suppose le pire cas.
            duration = cap
            unknown += 1
        durations.append(duration)
        total += estimate_video_cost(gen, duration, v.get("platform") or "tiktok")

    n = len(pending)
    avg_duration = sum(durations) / n if n else 0.0
    # Facteurs de reessai observables en production : blocages de securite et
    # rebuts qualite. Voir README pour le detail du modele de cout.
    retry_factor = 1.5

    return {
        "count": n,
        "avg_duration_s": round(avg_duration, 1),
        "duration_cap_s": cap,
        "unknown_durations": unknown,
        "unit_usd": round(total / n, 4) if n else 0.0,
        "unit_with_retries_usd": round(total / n * retry_factor, 4) if n else 0.0,
        "total_usd": round(total, 2),
        "total_with_retries_usd": round(total * retry_factor, 2),
    }


@app.post("/api/jobs/{job_id}/run")
async def job_run(job_id: str, req: StartRunRequest) -> dict:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    if not db.get_reference(req.generation.reference_image_id):
        raise HTTPException(400, "Image de reference introuvable")

    try:
        pipeline.launch(job_id, pipeline.run_job(job_id, req.generation))
    except PipelineError as exc:
        raise HTTPException(409, exc.message) from exc
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pause")
async def job_pause(job_id: str) -> dict:
    """Met la generation en pause.

    Les taches deja soumises a Kling continuent chez eux -- leur `task_id` est
    enregistre, la reprise recuperera les resultats sans repayer.
    """
    stopped = pipeline.request_cancel(job_id)
    if stopped:
        db.update_job(job_id, status=JobStatus.PAUSED)
    return {"ok": stopped}


@app.post("/api/jobs/{job_id}/resume")
async def job_resume(job_id: str) -> dict:
    """Repart avec les parametres de generation deja enregistres pour ce job."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable")
    if not job.get("generation"):
        raise HTTPException(
            400,
            "Aucun parametre de generation enregistre pour ce job. "
            "Relance depuis l'etape Generation.",
        )
    gen = GenerationParams(**job["generation"])
    try:
        pipeline.launch(job_id, pipeline.run_job(job_id, gen))
    except PipelineError as exc:
        raise HTTPException(409, exc.message) from exc
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def job_retry(job_id: str) -> dict:
    """Remet les videos en echec a leur etape precedente pour un nouvel essai.

    Les blocages de securite ne sont pas rejoues : le modele redonnerait le meme
    refus. Ils restent en echec tant que le prompt n'a pas change.
    """
    failed = db.list_videos(job_id, states=[VideoState.FAILED])
    reset = 0
    for v in failed:
        if v.get("error_kind") == "safety_block":
            continue
        # On repart de l'etape la plus avancee dont l'artefact existe deja,
        # pour ne pas repayer ce qui a reussi.
        if v.get("edited_path") and Path(v["edited_path"]).exists():
            state = VideoState.EDITED
        elif v.get("frame_path") and Path(v["frame_path"]).exists():
            state = VideoState.FRAMED
        elif v.get("local_path") and Path(v["local_path"]).exists():
            state = VideoState.DOWNLOADED
        else:
            state = VideoState.DISCOVERED
        db.set_state(v["id"], state, attempts=0, error=None, error_kind=None)
        reset += 1
    return {"ok": True, "reset": reset}


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------


@app.post("/api/videos/{video_id}/prepare")
async def video_prepare(video_id: str) -> dict:
    """Rend la video source lisible, en la telechargeant si besoin.

    Au moment de la validation, une video scrapee n'existe que sous forme de
    metadonnees : seule sa frame est sur disque. Pour la previsualiser il faut
    donc rapatrier le fichier. C'est gratuit -- aucune API facturee n'est
    sollicitee -- et le fichier atterrit la ou le pipeline l'attend, donc la
    generation n'aura pas a le retelecharger.
    """
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video introuvable")

    existing = video.get("local_path")
    if existing and Path(existing).exists() and Path(existing).stat().st_size > 0:
        return {"ready": True, "downloaded": False}

    try:
        dest = await pipeline.ensure_local_source(video["job_id"], video)
    except PipelineError as exc:
        raise HTTPException(502, exc.message) from exc
    except Exception as exc:  # noqa: BLE001 - reseau, CDN, yt-dlp...
        raise HTTPException(502, f"Téléchargement impossible : {exc}") from exc

    # On enregistre le chemin sans changer d'etat : la video reste « a valider »
    # tant que l'utilisateur n'a pas tranche.
    db.update_video(video_id, local_path=str(dest))
    return {"ready": True, "downloaded": True}


@app.get("/api/videos/{video_id}/{kind}")
async def video_file(video_id: str, kind: str) -> FileResponse:
    video = db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video introuvable")
    field = {
        "frame": "frame_path",
        "edited": "edited_path",
        "output": "output_path",
        "source": "local_path",
    }.get(kind)
    if not field:
        raise HTTPException(400, "Type de fichier inconnu")
    path = video.get(field)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Fichier absent")
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/download")
async def job_download(job_id: str) -> StreamingResponse:
    """Archive zip de toutes les videos finales du job."""
    videos = db.list_videos(job_id, states=[VideoState.DONE])
    if not videos:
        raise HTTPException(404, "Aucune video terminee")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for v in videos:
            path = Path(v["output_path"])
            if path.exists():
                name = f"{v['platform']}_{v['account']}_{v['external_id']}.mp4"
                zf.write(path, arcname=name)
    buf.seek(0)

    job = db.get_job(job_id) or {}
    filename = f"{(job.get('name') or job_id).replace(' ', '_')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/public/{token}/{path:path}")
async def public_asset(token: str, path: str) -> FileResponse:
    """Sert les fichiers aux serveurs de Kling, derriere un jeton."""
    if token != settings.resolved_asset_token():
        raise HTTPException(403, "Jeton invalide")
    try:
        target = resolve_public_path(path)
    except AssetHostError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Fichier introuvable")
    return FileResponse(target)


# ---------------------------------------------------------------------------
# Flux temps reel
# ---------------------------------------------------------------------------


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    async def gen():
        q = bus.subscribe()
        try:
            yield sse({"type": "hello"})
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield sse(payload)
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
