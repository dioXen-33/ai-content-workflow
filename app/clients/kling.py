"""Client Kling 3.0 Motion Control, API officielle.

Aligne sur la doc "Motion Control - KlingAI Open Platform" :

  Creation : POST /motion-control/kling-3.0
             corps = { contents: [{type:prompt|image|video, ...}], settings: {...} }
  Polling  : GET  /tasks?task_ids=<id>
             reponse = { code, data: [{ id, status, outputs:[{type:video,url}] }] }

Authentification, deux schemas (voir _auth_header) :
  - Cle unique (console kling.ai/dev/api-key) : envoyee telle quelle en Bearer.
  - Paire AccessKey/SecretKey (ancienne Open Platform) : JWT HS256 signe.

La duree de sortie n'est pas un parametre : la video generee fait la longueur de
la video de reference. Pour raccourcir, on tronque la source avant l'envoi.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt

from ..config import settings
from ..models import PRICING, FailureKind, PipelineError

POLL_INTERVAL = 10.0
MAX_WAIT = 1800.0

# Codes d'erreur documentes de la plateforme Kling.
_QUOTA_CODES = {1102, 1103, 1113, 1303, 1304}
_AUTH_CODES = {1000, 1001, 1002, 1003, 1004}


def _auth_header() -> dict[str, str]:
    """En-tete d'authentification, selon le schema disponible.

    - Cle unique (console kling.ai/dev/api-key) : envoyee telle quelle en Bearer.
    - Paire AccessKey/SecretKey (ancienne Open Platform) : JWT HS256 signe,
      valable 30 min, regenere a chaque requete.
    """
    if settings.kling_api_key:
        return {
            "Authorization": f"Bearer {settings.kling_api_key}",
            "Content-Type": "application/json",
        }

    if not settings.kling_access_key or not settings.kling_secret_key:
        raise PipelineError(
            FailureKind.INVALID_INPUT,
            "Aucune cle Kling : renseigne KLING_API_KEY, ou la paire "
            "KLING_ACCESS_KEY + KLING_SECRET_KEY.",
        )
    now = int(time.time())
    token = jwt.encode(
        {"iss": settings.kling_access_key, "exp": now + 1800, "nbf": now - 5},
        settings.kling_secret_key,
        algorithm="HS256",
        headers={"alg": "HS256", "typ": "JWT"},
    )
    if isinstance(token, bytes):  # PyJWT < 2 renvoyait des bytes
        token = token.decode("ascii")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _endpoint() -> str:
    return settings.kling_base_url.rstrip("/") + settings.kling_motion_control_path


def _classify(code: int, http_status: int, message: str) -> FailureKind:
    if code in _QUOTA_CODES:
        return FailureKind.QUOTA
    if code in _AUTH_CODES or http_status in (401, 403):
        return FailureKind.INVALID_INPUT
    if http_status == 429 or http_status >= 500:
        return FailureKind.TRANSIENT
    low = message.lower()
    if any(k in low for k in ("quota", "balance", "insufficient", "credit")):
        return FailureKind.QUOTA
    if any(k in low for k in ("risk", "violat", "sensitive", "forbidden content")):
        return FailureKind.SAFETY_BLOCK
    if 400 <= http_status < 500:
        return FailureKind.INVALID_INPUT
    return FailureKind.UNKNOWN


def _unwrap(resp: httpx.Response) -> dict:
    """Deballe l'enveloppe {code, message, data} de l'API Kling."""
    try:
        body = resp.json()
    except Exception:
        raise PipelineError(
            FailureKind.TRANSIENT if resp.status_code >= 500 else FailureKind.UNKNOWN,
            f"Reponse Kling illisible ({resp.status_code}) : {resp.text[:300]}",
        )

    code = int(body.get("code", 0) or 0)
    message = str(body.get("message", "") or "")

    if resp.status_code >= 400 or code != 0:
        raise PipelineError(
            _classify(code, resp.status_code, message),
            f"Kling a renvoye code={code} http={resp.status_code} : {message} "
            f"| corps : {resp.text[:400]}",
        )
    return body.get("data") or {}


def _resolution(mode: str) -> str:
    """Notre `mode` logique -> parametre `resolution` de l'API.

    L'API 3.0 ne parle plus de std/pro mais de resolution ; la tarification suit
    (720p ~ ancien std, 1080p ~ ancien pro).
    """
    return "1080p" if mode == "pro" else "720p"


async def submit(
    image_source: str,
    video_url: str,
    prompt: str,
    mode: str = "pro",
    keep_audio: bool = True,
    external_task_id: str | None = None,
) -> str:
    """Soumet une tache de motion control. Renvoie le task_id systeme.

    Format aligne sur POST /motion-control/kling-3.0 : un tableau `contents`
    d'entrees typees (prompt / image / video) et un objet `settings`.

    `image_source` : URL publique OU data URI base64 (data:image/...;base64,...).
    `video_url`    : URL HTTPS publique, telechargeable par les serveurs Kling.

    La duree de sortie n'est pas un argument : elle suit la video de reference.
    L'orientation est toujours `video` : le personnage reprend l'orientation de
    la video de reference, ce qui autorise jusqu'a 30 s de reference.
    """
    contents: list[dict] = []
    if prompt:
        contents.append({"type": "prompt", "text": prompt})
    contents.append({"type": "image", "url": image_source})
    contents.append({"type": "video", "url": video_url})

    payload: dict = {
        "contents": contents,
        "settings": {
            "character_orientation": "video",
            "audio": "original" if keep_audio else "off",
            "resolution": _resolution(mode),
        },
    }
    if external_task_id:
        payload["external_task_id"] = external_task_id

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=20.0)) as client:
        try:
            resp = await client.post(_endpoint(), headers=_auth_header(), json=payload)
        except httpx.HTTPError as exc:
            raise PipelineError(
                FailureKind.TRANSIENT, f"Kling injoignable : {exc}"
            ) from exc

    data = _unwrap(resp)
    task_id = data.get("id") or data.get("task_id")
    if not task_id:
        raise PipelineError(
            FailureKind.UNKNOWN, f"Aucun task id dans la reponse Kling : {data}"
        )
    return str(task_id)


def _tasks_endpoint() -> str:
    return settings.kling_base_url.rstrip("/") + settings.kling_tasks_path


async def poll(task_id: str) -> dict:
    """Interroge une tache via GET /tasks?task_ids=<id>.

    Renvoie {status, video_url, duration, error}.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0)) as client:
        try:
            resp = await client.get(
                _tasks_endpoint(),
                headers=_auth_header(),
                params={"task_ids": task_id},
            )
        except httpx.HTTPError as exc:
            raise PipelineError(
                FailureKind.TRANSIENT, f"Kling injoignable au polling : {exc}"
            ) from exc

    data = _unwrap(resp)
    # `data` est une liste de taches ; on prend la premiere.
    task = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
    status = str(task.get("status") or "").lower()

    video_url = None
    duration = None
    for out in task.get("outputs") or []:
        if isinstance(out, dict) and out.get("type") == "video" and out.get("url"):
            video_url = out["url"]
            duration = out.get("duration")
            break

    return {
        "status": status,
        "video_url": video_url,
        "duration": duration,
        "error": task.get("message") or "",
    }


async def wait_for_result(task_id: str, on_tick=None) -> str:
    """Attend la fin d'une tache et renvoie l'URL de la video."""
    waited = 0.0
    while waited < MAX_WAIT:
        info = await poll(task_id)
        status = info["status"]

        if status in ("succeed", "success", "succeeded", "completed"):
            if not info["video_url"]:
                raise PipelineError(
                    FailureKind.UNKNOWN,
                    f"Tache {task_id} reussie mais sans URL de video.",
                )
            return info["video_url"]

        if status in ("failed", "fail", "error"):
            message = info["error"] or "sans detail"
            kind = (
                FailureKind.SAFETY_BLOCK
                if any(k in message.lower() for k in ("risk", "violat", "sensitive"))
                else FailureKind.UNKNOWN
            )
            raise PipelineError(kind, f"Tache Kling {task_id} en echec : {message}")

        if on_tick:
            on_tick(status, waited)

        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

    raise PipelineError(
        FailureKind.TRANSIENT,
        f"Tache Kling {task_id} toujours en cours apres {MAX_WAIT / 60:.0f} min. "
        f"Elle continue cote Kling : le task_id est enregistre, une relance du job "
        f"reprendra le polling sans repayer.",
    )


def cost_for(duration_s: float, mode: str) -> float:
    """Kling facture a la seconde produite, donc a la seconde de la reference."""
    return PRICING["kling_motion_control"].get(mode, 0.1134) * duration_s


async def check_credentials() -> tuple[bool, str]:
    """Diagnostic : credentials valides et endpoint joignable ?

    On sonde le polling d'un task_id volontairement inexistant. Une erreur
    d'authentification signale des cles invalides ; un 404 applicatif signale au
    contraire que l'authentification passe et que le chemin repond.
    """
    has_single = bool(settings.kling_api_key)
    has_pair = bool(settings.kling_access_key and settings.kling_secret_key)
    if not has_single and not has_pair:
        return False, "Aucune cle Kling (KLING_API_KEY ou AccessKey+SecretKey)"
    scheme = "cle unique (Bearer)" if has_single else "AccessKey/SecretKey (JWT)"
    try:
        await poll("diagnostic-nonexistent-task")
        return True, f"Endpoint joignable, authentification acceptee ({scheme})"
    except PipelineError as exc:
        if exc.kind == FailureKind.INVALID_INPUT and "http=401" in exc.message:
            return False, f"Authentification refusee : {exc.message[:300]}"
        if exc.kind == FailureKind.INVALID_INPUT:
            # 400/404 sur un id bidon : l'auth est passee, l'endpoint existe.
            return True, (
                f"Authentification acceptee, endpoint {settings.kling_motion_control_path} "
                f"joignable (reponse attendue sur un id inexistant)."
            )
        return False, exc.message[:400]
    except Exception as exc:
        return False, str(exc)[:400]
