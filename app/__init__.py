"""Workflow IA - scraping IG/TikTok -> Nano Banana Pro -> Kling 3.0 Motion Control."""

__version__ = "1.0.0"

# Certains antivirus (Avast, Kaspersky, ESET...) et proxys d'entreprise
# inspectent le trafic HTTPS en le re-signant avec leur propre autorite racine.
# Cette racine est installee dans le magasin de certificats Windows, mais pas
# dans le bundle `certifi` qu'utilise Python par defaut : toutes les requetes
# echouent alors en CERTIFICATE_VERIFY_FAILED.
#
# `truststore` fait valider les certificats par le magasin du systeme, ce qui
# rend l'outil compatible avec ces environnements sans jamais desactiver la
# verification TLS.
try:  # pragma: no cover - dependant de l'environnement
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass
