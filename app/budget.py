"""Garde-fou budgetaire.

Une boucle de retry mal reglee sur Kling peut bruler plusieurs centaines de
dollars pendant une nuit. Chaque depense est enregistree avant l'appel, et le
pipeline refuse de demarrer une operation qui ferait franchir le plafond.
"""

from __future__ import annotations

from . import db
from .models import FailureKind, PipelineError


class BudgetExceeded(PipelineError):
    def __init__(self, spent: float, limit: float, needed: float):
        super().__init__(
            FailureKind.QUOTA,
            f"Plafond budgetaire atteint : {spent:.2f} USD depenses sur {limit:.2f} "
            f"autorises, l'operation suivante coute {needed:.2f} USD. Job mis en "
            f"pause. Releve MAX_SPEND_USD puis relance pour continuer.",
        )
        self.spent = spent
        self.limit = limit
        self.needed = needed


def guard(job_id: str, cost: float) -> None:
    """Verifie qu'une depense tient dans le plafond. Leve sinon."""
    job = db.get_job(job_id)
    if not job:
        return
    limit = float(job.get("max_spend_usd") or 0)
    spent = float(job.get("spent_usd") or 0)
    if limit <= 0:
        return
    if spent + cost > limit:
        raise BudgetExceeded(spent, limit, cost)


def charge(job_id: str, video_id: str | None, cost: float) -> float:
    """Enregistre une depense effective."""
    if cost <= 0:
        return 0.0
    total = db.add_spend(job_id, cost)
    if video_id:
        video = db.get_video(video_id)
        if video:
            db.update_video(video_id, cost_usd=float(video.get("cost_usd") or 0) + cost)
    return total


def summary(job_id: str) -> dict:
    job = db.get_job(job_id) or {}
    limit = float(job.get("max_spend_usd") or 0)
    spent = float(job.get("spent_usd") or 0)
    return {
        "spent_usd": round(spent, 4),
        "limit_usd": limit,
        "remaining_usd": round(max(limit - spent, 0), 4) if limit else None,
        "pct": round(spent / limit * 100, 1) if limit else None,
    }
