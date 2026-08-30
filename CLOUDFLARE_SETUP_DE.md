# Cloudflare Scheduler für Weatherman Madrid v1.0.4

## Ziel

Cloudflare erzeugt im aktiven Madrid-Fenster alle 15 Minuten einen gezielten
GitHub-`workflow_dispatch` mit einem expliziten UTC-Soll-Slot. Um 21:15 Europe/Madrid
wird zusätzlich der Tagesabschluss ausgelöst. GitHub-Cron bleibt als stündliches
Sicherheitsnetz aktiv.

Cloudflare erhält niemals Neon-, Meteoblue- oder Streamlit-Zugangsdaten. Der Worker
bekommt ausschließlich einen auf ein Repository begrenzten GitHub-Token, der Workflows
starten darf.

## Teil A – zuerst v1.0.4 in GitHub installieren

1. Lade das v1.0.4-Paket herunter und entpacke es.
2. Lade den Inhalt von `UPLOAD_TO_GITHUB` in die Wurzel des vorhandenen Repositorys
   `weatherman84/weatherman-madrid`.
3. Kontrolliere besonders den versteckten Ordner `.github/workflows`. Dort müssen
   anschließend diese Dateien liegen:
   - `madrid-collector.yml`;
   - `publish-daily-analysis-export.yml`;
   - `madrid-closeout.yml`.
4. Falls der Browser den versteckten Ordner wieder auslässt, lade die drei im Paket
   zusätzlich bereitgestellten Workflow-Dateien einzeln direkt unter
   `.github/workflows` hoch.
5. Commit-Nachricht: `Weatherman Madrid v1.0.4 – cadence freshness`.
6. Warte auf **0 – Tests**. Alle Tests müssen grün sein.
7. Öffne **Actions → 6 – Publish Madrid daily-analysis export** und starte den Workflow
   einmal manuell. Build, Deploy und die Prüfung der veröffentlichten Datei müssen grün
   sein.

Cloudflare erst einrichten, nachdem die neuen Workflows auf dem GitHub-`main`-Branch
liegen. Andernfalls kann GitHub die von Cloudflare genannten Workflow-Dateien noch
nicht finden.

## Teil B – eingeschränkten GitHub-Token erstellen

1. Öffne GitHub und gehe über dein Profilbild zu
   **Settings → Developer settings → Personal access tokens → Fine-grained tokens**.
2. Wähle **Generate new token**.
3. Empfohlene Angaben:
   - Token name: `weatherman-cloudflare-dispatch`;
   - Expiration: möglichst lang, beispielsweise 12 Monate, sofern GitHub dies erlaubt;
   - Resource owner: `weatherman84`;
   - Repository access: **Only select repositories**;
   - ausgewähltes Repository: **weatherman-madrid**.
4. Unter **Repository permissions** setze ausschließlich:
   - **Actions: Read and write**.
5. Alle anderen veränderbaren Berechtigungen bleiben auf **No access**. Die automatisch
   gesetzte Metadata-Leseberechtigung ist normal.
6. Erzeuge den Token und kopiere ihn einmalig. Poste ihn niemals in ChatGPT, GitHub-Code,
   Issues oder Workflow-Logs.

Der Token kann nur Actions im ausgewählten Repository auslösen. Er erhält keinen
Schreibzugriff auf Repository-Inhalte und keinen Zugriff auf Neon oder Meteoblue.

## Teil C – kostenlosen Cloudflare Worker anlegen

1. Öffne <https://dash.cloudflare.com/> und erstelle bei Bedarf ein kostenloses Konto.
2. Öffne **Workers & Pages**.
3. Wähle **Create** beziehungsweise **Create application**, danach **Worker**.
4. Worker-Name: `weatherman-madrid-scheduler`.
5. Erstelle den Worker und öffne anschließend **Edit code**.
6. Ersetze den Beispielcode vollständig durch den Inhalt von:
   `cloudflare-scheduler/src/index.js`.
7. Wähle **Deploy**.

Für diese Lösung wird keine Domain, Datenbank, KV-Instanz oder kostenpflichtige
Cloudflare-Funktion benötigt.

## Teil D – Variablen und GitHub-Token hinterlegen

1. Öffne im Worker **Settings → Variables and Secrets**.
2. Lege diese normalen Textvariablen an:

   | Name | Wert |
   |---|---|
   | `GITHUB_OWNER` | `weatherman84` |
   | `GITHUB_REPO` | `weatherman-madrid` |
   | `GITHUB_REF` | `main` |

3. Lege zusätzlich eine Variable mit Typ **Secret** an:

   | Name | Wert |
   |---|---|
   | `GITHUB_TOKEN` | der eben erzeugte Fine-grained GitHub-Token |

4. Achte ausdrücklich auf den Typ **Secret**, nicht Plaintext.
5. Wähle **Deploy**, damit die Variablen in der aktiven Worker-Version verfügbar sind.

## Teil E – Cron Trigger einrichten

1. Öffne den Worker und gehe zu **Settings → Triggers → Cron Triggers**.
2. Füge diesen Collector-Trigger hinzu:

   `7,22,37,52 5-20 * * *`

3. Füge diesen DST-sicheren Tagesabschluss-Trigger hinzu:

   `15 19,20 * * *`

4. Speichere beide Trigger.

Cloudflare-Cron verwendet UTC. Der erste Trigger erzeugt die vier 15-Minuten-Slots
zwischen 05:00 und 20:59 UTC. Der zweite wird zweimal täglich aufgerufen. Der Worker
rechnet beide vorgesehenen UTC-Zeitpunkte nach Europe/Madrid um und dispatcht nur
denjenigen, der tatsächlich 21:15 Madrid-Zeit entspricht. Damit funktioniert die
Umstellung zwischen Sommer- und Winterzeit ohne manuelle Änderung.

## Teil F – Verbindung prüfen

1. Warte auf den nächsten Zeitpunkt mit Minute 07, 22, 37 oder 52 innerhalb von
   05:00–20:59 UTC.
2. Öffne in GitHub **Actions → 3 – Madrid collector**.
3. Ein neuer Lauf muss erscheinen:
   - Event: `workflow_dispatch`;
   - Input `source`: `cloudflare`;
   - Input `scheduled_slot`: exakter UTC-Zeitpunkt des Cron-Slots.
4. Der Lauf muss grün enden. Im Collector-Ergebnis muss der übergebene
   `scheduled_at`-Wert stehen.
5. Öffne in Cloudflare den Worker und prüfe unter **Logs** beziehungsweise
   **Settings → Trigger Events → View events** den Status `dispatched`.

Cloudflare kann neue Cron-Ereignisse in der Verlaufsansicht zeitverzögert anzeigen.
Für die Funktionsprüfung ist der tatsächlich erzeugte GitHub-Workflow-Run maßgeblich.

## Teil G – Tagesabschluss prüfen

Nach dem ersten 21:15-LT-Lauf:

1. Öffne GitHub **Actions → 7 – Madrid day closeout**.
2. Der Lauf muss zuerst `closeout` und anschließend `publish` erfolgreich abschließen.
3. Öffne:
   <https://weatherman84.github.io/weatherman-madrid/daily-analysis-latest.json>
4. `generated_at` muss zum aktuellen Tagesabschluss gehören.
5. `window.last_target_date` muss dem aktuellen Madrid-Datum entsprechen.
6. Falls GitHub Pages noch die alte Datei ausliefert, schlägt die neue
   Deployment-Prüfung fehl, statt einen falschen grünen Erfolg zu melden.

## Sicherheits- und Wartungshinweise

- GitHub-Token niemals in `wrangler.jsonc`, JavaScript, GitHub-Secrets oder ChatGPT
  einfügen. Er gehört ausschließlich als Cloudflare-Secret `GITHUB_TOKEN` hinterlegt.
- Wenn der Token abläuft oder widerrufen wird, meldet der Worker einen GitHub-Fehler
  401 beziehungsweise 403 und es entstehen keine Cloudflare-Dispatches mehr.
- Vor Ablauf einen neuen Fine-grained Token mit denselben Minimalrechten erstellen und
  nur das Cloudflare-Secret aktualisieren.
- GitHub-Cron bleibt stündlich aktiv und bietet bei einem Cloudflare-Ausfall weiterhin
  Grundabdeckung.
- Gleiche Soll-Slots werden in Neon idempotent behandelt. Ein paralleler Cloudflare-
  und GitHub-Aufruf erzeugt deshalb keinen doppelten produktiven Collector-Lauf.

## Abnahmekriterien

Die Pipeline gilt erst nach zwei vollständigen Madrid-Tagen als repariert:

- mindestens 90 % der vorgesehenen Slots;
- alle vier festen Checkpoints vorhanden;
- kein Checkpoint ausschließlich wegen Scheduler-Drift rekonstruiert;
- aktueller Tagesabschluss-Export;
- vollständiger Actual-Pfad;
- Providerfehler separat gekennzeichnet;
- keine Änderung an der Forecast-Engine.

## Optionale CLI-Installation

Wer Cloudflare lieber reproduzierbar per Terminal bereitstellt:

1. Im Ordner `cloudflare-scheduler` ausführen: `npm install`.
2. Cloudflare-Anmeldung: `npx wrangler login`.
3. Secret setzen: `npx wrangler secret put GITHUB_TOKEN`.
4. Deployment einschließlich Cron Trigger: `npm run deploy`.

Die Dashboard-Anleitung oben ist für die einmalige Einrichtung ausreichend.
