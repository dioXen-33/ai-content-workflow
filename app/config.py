"""Configuration centralisee, chargee depuis .env."""

from __future__ import annotations

import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Scraping ------------------------------------------------------------
    # `apify` : API payante, robuste. `ytdlp` : gratuit, via le navigateur dedie.
    scraper_backend: str = "apify"

    # Navigateur de scraping isole (profil dedie, jamais le profil personnel).
    browser_executable: str = ""
    browser_debug_port: int = 9333

    # --- Apify ---------------------------------------------------------------
    apify_token: str = ""
    apify_instagram_actor: str = "apidojo~instagram-scraper"
    apify_tiktok_actor: str = "clockworks~tiktok-scraper"

    # --- Nano Banana Pro -----------------------------------------------------
    gemini_api_key: str = ""
    # Version GA plutot que `-preview` : les modeles preview recoivent moins de
    # capacite et sont les premiers a renvoyer des 503.
    gemini_image_model: str = "gemini-3-pro-image"
    # Modeles de repli, essayes dans l'ordre quand le principal est sature.
    # Vide par defaut : mieux vaut attendre Nano Banana Pro que produire une
    # image de moindre qualite. Les modeles Flash degradent nettement le rendu
    # (texte deforme, details approximatifs).
    gemini_fallback_models: str = ""

    @property
    def gemini_model_chain(self) -> list[str]:
        chain = [self.gemini_image_model]
        for name in self.gemini_fallback_models.split(","):
            name = name.strip()
            if name and name not in chain:
                chain.append(name)
        return chain

    # Batch API : 50 % du tarif interactif, cible de traitement a 24 h.
    gemini_batch_chunk_size: int = 250      # requetes par fichier JSONL
    gemini_batch_poll_interval: int = 60    # secondes entre deux verifications
    gemini_batch_max_wait_h: float = 26.0   # abandon du polling au-dela
    gemini_input_max_px: int = 1280         # redimensionnement des images envoyees

    # --- Kling ---------------------------------------------------------------
    # Deux schemas d'authentification selon la console :
    #  - Cle unique (kling.ai/dev/api-key) -> KLING_API_KEY, envoyee en Bearer.
    #  - Paire AccessKey/SecretKey (ancienne Open Platform) -> JWT signe.
    # Si KLING_API_KEY est renseignee, elle a la priorite.
    kling_api_key: str = ""
    kling_access_key: str = ""
    kling_secret_key: str = ""
    kling_base_url: str = "https://api-singapore.klingai.com"
    # Endpoint de creation de tache et endpoint de polling (doc officielle 3.0).
    kling_motion_control_path: str = "/motion-control/kling-3.0"
    kling_tasks_path: str = "/tasks"
    kling_mode: str = "pro"

    # --- Hebergement des fichiers -------------------------------------------
    asset_host_mode: str = "local"  # local | source
    public_base_url: str = ""
    asset_token: str = ""

    # --- Garde-fous ----------------------------------------------------------
    max_spend_usd: float = 50.0
    dry_run: bool = False

    # --- Concurrence ---------------------------------------------------------
    concurrency_download: int = 4
    concurrency_gemini: int = 6
    concurrency_kling: int = 3

    # --- Serveur -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: str = "data"

    # ------------------------------------------------------------------------
    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "workflow.db"

    @property
    def media_path(self) -> Path:
        p = self.data_path / "media"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def refs_path(self) -> Path:
        p = self.data_path / "references"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def resolved_public_base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    def resolved_asset_token(self) -> str:
        """Jeton d'acces aux fichiers publics.

        Genere et persiste au premier demarrage si absent du .env, pour que les
        URLs restent valides d'une session a l'autre.
        """
        if self.asset_token:
            return self.asset_token
        token_file = self.data_path / ".asset_token"
        if token_file.exists():
            return token_file.read_text(encoding="utf-8").strip()
        token = secrets.token_urlsafe(24)
        token_file.write_text(token, encoding="utf-8")
        return token

    # --- Diagnostics ---------------------------------------------------------
    def missing_keys(self) -> list[str]:
        """Cles indispensables a une execution reelle (hors dry-run)."""
        missing = []
        # Le token Apify n'est requis que si c'est le backend de scraping choisi.
        if self.scraper_backend == "apify" and not self.apify_token:
            missing.append("APIFY_TOKEN")
        if not self.gemini_api_key:
            missing.append("GEMINI_API_KEY")
        # Kling accepte l'un OU l'autre schema d'authentification.
        if not self.kling_api_key and not (
            self.kling_access_key and self.kling_secret_key
        ):
            missing.append("KLING_API_KEY (ou KLING_ACCESS_KEY + KLING_SECRET_KEY)")
        return missing

    def warnings(self) -> list[str]:
        warns = []
        if self.asset_host_mode == "local" and not self.public_base_url:
            warns.append(
                "ASSET_HOST_MODE=local sans PUBLIC_BASE_URL : Kling ne pourra pas "
                "telecharger les videos depuis localhost. Renseigne l'URL publique "
                "de ce serveur, ou bascule sur ASSET_HOST_MODE=source."
            )
        if self.asset_host_mode == "source":
            warns.append(
                "ASSET_HOST_MODE=source : les URLs CDN Instagram/TikTok sont signees "
                "et expirent. A n'utiliser que si la generation suit immediatement le "
                "scraping."
            )
        return warns


settings = Settings()
