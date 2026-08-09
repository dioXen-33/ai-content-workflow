"""Bundle de certificats pour les sous-processus (yt-dlp, ffmpeg...).

Le processus principal utilise `truststore` (voir app/__init__.py), qui delegue
la validation au magasin du systeme. Un sous-processus n'en herite pas : il faut
lui designer un fichier PEM via SSL_CERT_FILE.

On fabrique donc un bundle qui reunit :
  - les autorites publiques de `certifi` ;
  - les racines du magasin Windows, ce qui couvre les antivirus et proxys qui
    inspectent le HTTPS (Avast, Kaspersky, ESET, proxys d'entreprise...).

Sans cela, ces environnements provoquent des CERTIFICATE_VERIFY_FAILED sur tout
le trafic, alors que le navigateur, lui, fonctionne normalement.
"""

from __future__ import annotations

import ssl
import time
from pathlib import Path

from .config import settings

_MAX_AGE_S = 7 * 24 * 3600


def _system_pems() -> list[str]:
    pems: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            for der, encoding, _trust in ssl.enum_certificates(store):
                if encoding == "x509_asn":
                    try:
                        pems.append(ssl.DER_cert_to_PEM_cert(der))
                    except Exception:
                        continue
        except (AttributeError, OSError):
            # `enum_certificates` n'existe que sous Windows.
            break
    return pems


def bundle_path() -> Path:
    """Chemin du bundle combine, regenere si absent ou perime."""
    path = settings.data_path / "ca_bundle.pem"
    if path.exists() and (time.time() - path.stat().st_mtime) < _MAX_AGE_S:
        return path

    parts: list[str] = []
    try:
        import certifi

        parts.append(Path(certifi.where()).read_text(encoding="utf-8"))
    except Exception:
        pass
    parts.extend(_system_pems())

    if parts:
        path.write_text("\n".join(parts), encoding="utf-8")
    return path
