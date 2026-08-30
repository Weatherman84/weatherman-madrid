# Weatherman Madrid v1.0.4 aktualisieren

Diese Version ersetzt ausschließlich die Modell-Freshness-Logik und übernimmt alle
Scheduler-, Export- und Performance-Fixes aus v1.0.3.

## Upload

1. ZIP `weatherman-madrid-v1.0.4.zip` entpacken.
2. Ordner `UPLOAD_TO_GITHUB` öffnen.
3. Den gesamten **Inhalt** dieses Ordners in die Wurzel des bestehenden Repositorys
   `weatherman84/weatherman-madrid` hochladen.
4. Vorhandene Dateien ersetzen lassen.
5. Commit-Nachricht: `Weatherman Madrid v1.0.4 cadence-aware freshness`.

Der Workflow-Ordner wurde gegenüber v1.0.3 nicht geändert. Falls Windows oder der
Browser `.github` beim Upload auslässt, ist für dieses Update kein separater
Workflow-Upload erforderlich.

## Prüfung

1. Unter **Actions → 0 - Tests** auf den neuen Lauf warten.
2. Erwartetes Ergebnis: `216 passed` und Ruff grün.
3. Danach die Streamlit-App neu starten beziehungsweise auf das automatische Deployment
   warten.
4. Einmal **Refresh Madrid now** ausführen.
5. Unter **Model maxima, weights and freshness** kontrollieren:
   - GFS/ECMWF/ICON Global/UKMO/ARPEGE: Update cadence 360 min;
   - ICON-EU/AROME/AROME-HD: Update cadence 180 min;
   - `Current latest run` oder `Awaiting next run` bedeutet `Used = true`;
   - `Expected run missing` oder `Hard stale` bedeutet `Used = false`.

Cloudflare, Neon, GitHub Secrets und der Meteoblue-Key müssen nicht neu eingerichtet
werden.

## Evidenz-Neustart

Engine v10.7.11 kann andere Modelle in den Champion aufnehmen als v10.7.10. Deshalb
beginnt der sequenzielle OOS-Promotion-Zähler am 31. August 2026 neu. Ältere Tage bleiben
für Diagnostik und Analoge erhalten, zählen aber nicht für eine Promotion dieser Version.
