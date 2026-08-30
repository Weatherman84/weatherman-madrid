# Weatherman Madrid v1.0.4 – genaue Einrichtung

Diese Anleitung setzt keine Erfahrung mit Neon voraus. Arbeite die Schritte genau in
der Reihenfolge ab. Die alte App und das alte Repository werden dabei **nicht** verändert.

## 0. Was du bereithalten musst

- dein GitHub-Konto;
- dein bestehender Meteoblue-API-Key;
- Zugang zu [Streamlit Community Cloud](https://share.streamlit.io/);
- das gelieferte ZIP `weatherman-madrid-v1.0.4.zip`.

Wichtig: Eine Neon-Verbindungszeichenfolge enthält ein Passwort. Poste sie nicht in
Chats, Issues oder Screenshots. Sie wird ausschließlich in GitHub Secrets und in den
Streamlit Secrets eingefügt.

## 1. ZIP entpacken

1. Lade `weatherman-madrid-v1.0.4.zip` herunter.
2. Rechtsklick auf die Datei und **Alle extrahieren** wählen.
3. Öffne anschließend den Ordner `UPLOAD_TO_GITHUB`.
4. Darin müssen unter anderem `app.py`, `README.md`, `src`, `config`, `.github` und
   `.streamlit` sichtbar sein.

Das Paket bleibt unter 100 hochzuladenden Dateien. Es enthält keine SQLite-Datenbank,
keine historischen Einzeldateien, keinen Cache und keine Zugangsdaten.

## 2. Neues GitHub-Repository erstellen

1. Öffne [github.com/new](https://github.com/new).
2. Bei **Repository name** trägst du `weatherman-madrid` ein.
3. Wähle **Public**. Das ist hier empfohlen, weil Standard-GitHub-Runner für öffentliche
   Repositories kostenlos sind. Die App wird dadurch standardmäßig ebenfalls öffentlich.
4. Setze **keinen** Haken bei README, `.gitignore` oder License. Das Repository soll leer
   erstellt werden, weil diese Dateien bereits im Paket liegen.
5. Klicke **Create repository**.
6. Klicke auf der leeren Repository-Seite auf **uploading an existing file**.
7. Öffne lokal `UPLOAD_TO_GITHUB`, markiere dessen gesamten Inhalt mit `Strg+A` und ziehe
   alles in das GitHub-Uploadfeld. Ziehe den **Inhalt**, nicht den übergeordneten Ordner.
8. Kontrolliere vor dem Commit, dass `app.py` direkt auf Repository-Ebene liegt und nicht
   in einem zusätzlichen Unterordner.
9. Bei Commit message trägst du `Weatherman Madrid v1.0.4` ein.
10. Klicke **Commit changes**.

Wenn `.github` oder `.streamlit` fehlen, den Upload noch nicht abschließen. Beide Ordner
sind notwendig.

## 3. Produktionsdatenbank in Neon erstellen

Der aktuelle Neon-Free-Plan reicht für zwei getrennte kleine Projekte aus. Neon weist
laut aktueller Preisseite jedem Free-Projekt ein eigenes Speicher- und Compute-Kontingent
zu und skaliert bei Inaktivität auf null.

1. Öffne [console.neon.tech](https://console.neon.tech/) und melde dich an, am einfachsten
   mit GitHub.
2. Klicke **New project**.
3. Project name: `weatherman-madrid-prod`.
4. Region: falls angeboten, eine europäische Region nahe Frankfurt wählen. Sonst die
   nächstgelegene europäische Region.
5. PostgreSQL-Version und Datenbankname können auf dem Standard bleiben.
6. Klicke **Create project**.
7. Öffne im Projekt **Connection Details** beziehungsweise **Connect**.
8. Aktiviere **Pooled connection**. Im Hostnamen sollte normalerweise `-pooler` stehen.
9. Kopiere die vollständige Zeichenfolge, beginnend mit `postgresql://` und inklusive
   `sslmode=require`. Nenne sie für dich vorübergehend `PROD_DATABASE_URL`.
10. Lass Scale-to-zero und die Free-Compute-Einstellungen aktiv. Eine dauerhaft laufende
    Compute-Instanz ist für diese App nicht nötig.

## 4. Separate Replay-Datenbank in Neon erstellen

1. Gehe in Neon zurück zur Projektübersicht.
2. Klicke erneut **New project**.
3. Project name: `weatherman-madrid-replay`.
4. Wähle wieder eine europäische Region.
5. Erstelle das Projekt und kopiere auch hier die **pooled** Connection String.
6. Nenne diese zweite Zeichenfolge `REPLAY_DATABASE_URL`.

Nutze nicht bloß eine zweite Tabelle oder einen Branch der Produktionsdatenbank. Zwei
eigenständige Neon-Projekte geben dem Replay eine klare technische Schreibgrenze und ein
eigenes Free-Kontingent.

## 5. GitHub-Secrets anlegen

1. Öffne dein neues Repository `weatherman-madrid`.
2. Gehe zu **Settings**.
3. Links: **Secrets and variables** → **Actions**.
4. Klicke **New repository secret** und lege nacheinander exakt diese drei Secrets an:

| Name | Inhalt |
|---|---|
| `DATABASE_URL` | pooled Connection String aus `weatherman-madrid-prod` |
| `REPLAY_DATABASE_URL` | pooled Connection String aus `weatherman-madrid-replay` |
| `METEOBLUE_API_KEY` | dein vorhandener Meteoblue-Key |

Keine Anführungszeichen und keine Leerzeichen vor oder nach dem Wert einfügen. Ein
`NEON_API_KEY` ist nicht erforderlich.

## 6. Tests und Datenbank-Workflows ausführen

1. Öffne im Repository den Reiter **Actions**.
2. Falls GitHub nachfragt, klicke **I understand my workflows, go ahead and enable them**.
3. Warte zuerst auf den Workflow **0 - Tests**. Er muss grün sein.
4. Öffne **1 - Initialize Neon database** → **Run workflow** → nochmals **Run workflow**.
5. Warte, bis der Lauf grün ist. Dieser Schritt erstellt nur das Schema in der
   Produktionsdatenbank.
6. Öffne **2 - Import Madrid history** und starte ihn einmal manuell.
7. Warte auf Grün. Dieser Workflow lädt aus dem alten öffentlichen Repository nur den
   aktuellen Daten-Snapshot, filtert ausschließlich `LEMD` und importiert ihn nach Neon.
8. Öffne **3 - Madrid collector** und starte ihn einmal manuell.
9. Prüfe im Log am Ende `status: success`. Ab jetzt läuft er automatisch.

Die Workflows 1 bis 3 nicht gleichzeitig starten. Die Schreib-Workflows besitzen zwar
eine gemeinsame Sperre, die Reihenfolge macht die Ersteinrichtung aber nachvollziehbar.

## 7. Replay-Lab vorbereiten und 30 Tage ausführen

1. Starte **4 - Prepare isolated replay lab**.
2. Warte auf Grün. Der Workflow prüft, dass Production und Replay wirklich verschiedene
   Datenbanken sind. Production wird dabei nur in einer Read-only-Transaktion geprüft.
3. Starte anschließend **5 - Run 30-day Madrid replay**.
4. Lass bei `days` den Wert `30` stehen und bestätige **Run workflow**.
5. Nach erfolgreichem Abschluss öffnest du den Lauf.
6. Scrolle zum Bereich **Artifacts** und lade `madrid-replay-report` herunter.
7. Entpacke die Datei; darin liegt `madrid-replay-report.json`.

Der Report trennt:

- `historical-causal`;
- `reconstructed-research`;
- `unavailable`.

Ein kleines `N` oder viele `unavailable`-Fälle sind kein technischer Fehler. Sie zeigen,
dass die alte Pipeline am jeweiligen Zeitpunkt nicht genügend echte historische
Snapshots gespeichert hatte. Solche Tage werden nicht künstlich als OOS-Evidenz gezählt.
Der Replay schreibt ausschließlich in `weatherman-madrid-replay`, rollt seine
Production-Transaktion zurück und kann keine Engine-Regel promoten.

## 7a. Read-only-Export für die tägliche Madrid-Analyse aktivieren

Dieser Schritt gibt der täglichen Research-Analyse Zugriff auf die benötigten Neon-Daten,
ohne Zugangsdaten weiterzugeben und ohne der Analyse Schreibrechte einzuräumen.

1. Öffne **Actions** → **6 - Publish Madrid daily-analysis export**.
2. Klicke **Run workflow** → **Run workflow**.
3. Warte, bis sowohl `build` als auch `deploy` grün sind.
4. Öffne danach
   `https://weatherman84.github.io/weatherman-madrid/daily-analysis-latest.json`.
5. Wenn dort JSON mit `"airport": "LEMD"` und
   `"classification": "READ-ONLY DAILY ANALYSIS EXPORT"` erscheint, ist der Zugriff
   fertig eingerichtet.

Der vorhandene GitHub-Secret `DATABASE_URL` genügt; in Neon muss nichts manuell geändert
werden. Der Workflow läuft anschließend einmal täglich um 21:15 Uhr Madrid-Zeit. Zwei
UTC-Cronzeiten decken Sommer- und Winterzeit ab; ein lokaler Zeit-Guard veröffentlicht
pro Tag nur einmal. GitHub Pages ist öffentlich: Der Workflow prüft deshalb vor dem
Upload, dass keine Datenbank-Credentials enthalten sind, und bricht bei einem Treffer ab.

Der Export enthält sieben Tage sowie den Folgetag für D−1-Checkpoints. Damit kann die um
21:30 Uhr laufende Madrid-Analyse Forecast Ladder, Evidence/Freshness, Forecast Drivers,
Regime- und Adjustment-Impacts, Challenger, TAF-Provenienz und Collector-Abdeckung direkt
auswerten.

## 8. Neue Streamlit-App erstellen

1. Öffne [share.streamlit.io](https://share.streamlit.io/) und melde dich an.
2. Falls noch nötig: GitHub-Konto verbinden und Streamlit Zugriff auf öffentliche
   Repositories erlauben.
3. Klicke oben rechts **Create app**.
4. Wähle **Yup, I have an app**.
5. Repository: `<dein-github-name>/weatherman-madrid`.
6. Branch: `main`.
7. Main file path: `app.py`.
8. Optionaler App-Name: zum Beispiel `weatherman-madrid`.
9. Öffne **Advanced settings**.
10. Python version: `3.12`.
11. Füge im Feld **Secrets** exakt dieses TOML ein und ersetze nur die zwei Platzhalter:

```toml
DATABASE_URL = "DEINE_POOLED_PROD_DATABASE_URL"
METEOBLUE_API_KEY = "DEIN_BESTEHENDER_METEOBLUE_KEY"
METEOBLUE_DAILY_CALL_LIMIT = "4"
EDGE_RECOMMENDATIONS_ENABLED = "false"
REGIME_MEMORY_ALLOW_PROMOTED = "false"
REGIME_MEMORY_AUTO_PROMOTION = "false"
```

`REPLAY_DATABASE_URL` kommt bewusst **nicht** in die Streamlit-App. Nur GitHub Actions
erhält Zugriff auf das Replay-Projekt.

12. Klicke **Save**, danach **Deploy**.
13. Der erste Build kann wegen der Python-Abhängigkeiten einige Minuten dauern. Im
    Normalfall sollte die App danach ohne Repository-Clone-Schleife starten, weil keine
    große DB und kein großes Datenarchiv im neuen Repository liegen.

## 9. Erste Funktionsprüfung

Nach dem Start prüfst du in dieser Reihenfolge:

1. Oben steht **Weatherman Madrid** und `Neon/PostgreSQL persistence`.
2. Klicke genau einmal **Refresh Madrid now**.
3. Es erscheinen Champion Maximum, Latest METAR, METAR Max und Temperature Trend.
4. Die Forecast Chain enthält Raw, Bias-corrected, Live weather-adjusted und Champion.
5. Die Bucket-Tabelle ist aufsteigend nach Temperatur sortiert.
6. Unter **Fixed decision checkpoints** siehst du vier feste Zeilen.
7. Unter **Model, TAF and Meteoblue diagnostics** werden Quellenalter und – nach einem
   Checkpoint-Fenster – die Meteoblue-Versuche angezeigt.

Wenn zunächst `fewer than two fresh model sources` erscheint, den manuellen Collector in
GitHub prüfen und danach einmal refreshen. Ein altes Modell wird nicht mehr stillschweigend
in den Champion aufgenommen.

## 10. Wann Reliability-N steigt

`N` steigt nicht bei jedem App-Aufruf und auch nicht sofort am selben Tag. Ein Tag zählt
erst, wenn gleichzeitig gilt:

- der feste Checkpoint wurde planmäßig und vor dem Peak gespeichert;
- ein finales `stored-metar-station`-Actual liegt vor;
- der Checkpoint war weder reconstructed noch late/post-peak;
- die nötigen Forecastwerte sind vorhanden.

Darum startet das neue N für 09:00, 12:00 und 16:00 zunächst bei null. Die App zeigt
separat, wie viele Fälle reconstructed, late/post-peak, missing oder provisional sind.
So ist ein stehenbleibendes N erklärbar und nicht automatisch ein Speicherfehler.

## 11. Meteoblue-Verhalten

Die Software reserviert höchstens einen Meteoblue-Versuch in jedem Fenster:

- 19:45–20:30 für D−1 @20:00;
- 08:00–09:30 für D0 @09:00;
- 11:45–12:30 für First Live @12:00;
- 15:45–16:30 für Late Live @16:00.

Das sind maximal vier HTTP-Aufrufe pro Madrider Kalendertag. Laut aktueller Meteoblue-
Dokumentation arbeitet die Free Weather API mit einem ein Jahr gültigen Credit-Kontingent;
die tatsächlichen Kosten hängen von den angefragten Paketen ab. Prüfe deshalb gelegentlich
im Meteoblue API Manager den realen Credit-Verbrauch. Bei `429`, `quota` oder `credit`
geht die App für 24 Stunden in Cooldown, statt den Key weiter zu belasten.

## 12. Free-Tier-Monitoring

Nach drei und nach sieben Tagen:

1. Neon öffnen → Projekt `weatherman-madrid-prod` → **Usage**.
2. Compute und Storage kontrollieren.
3. Das Produktionsprojekt enthält nur Madrid und sollte weit unter 0,5 GB bleiben.
4. Der aktive Collector läuft alle 15 Minuten, außerhalb stündlich. Neon soll zwischen
   kurzen Zugriffen auf null skalieren.
5. Falls der hochgerechnete Compute-Verbrauch ungewöhnlich hoch ist, die App nicht
   kostenpflichtig upgraden. Zuerst gemeinsam den aktiven Cron auf 20 oder 30 Minuten
   reduzieren.

Der Replay hat ein eigenes Projekt und ein eigenes Kontingent. Den Replay nicht täglich
automatisch ausführen; er ist absichtlich nur manuell startbar.

## 13. Parallelbetrieb und Rollback

1. Lass alte und neue App mindestens zwei bis drei Tage parallel laufen.
2. Vergleiche Champion, Latest METAR, Forecast Chain und Zeitstempel.
3. Prüfe, ob alle vier neuen Checkpoints gespeichert werden.
4. Erst wenn die neue App stabil ist, können die alten Collector-Workflows deaktiviert
   werden. Das alte Repository und die alte App werden nicht gelöscht.
5. Falls die neue App Probleme macht, nutze sofort wieder die alte URL. Weil beide
   Datenbanken getrennt sind, ist kein Datenbank-Rollback nötig.

## 14. Häufige Fehler

### `Database connection failed`

- In Streamlit Secrets fehlt `DATABASE_URL` oder sie enthält Tippfehler.
- Prüfe, ob die pooled URL vollständig inklusive Passwort und `sslmode=require` kopiert
  wurde.
- Secret korrigieren und App rebooten.

### Workflow 1 oder 2 ist rot

- GitHub Secret heißt nicht exakt `DATABASE_URL`.
- Falsche oder unvollständige Neon-URL.
- Den roten Schritt öffnen und nur die Fehlermeldung ohne Zugangsdaten weitergeben.

### Replay-Sicherheitsstopp

- Production und Replay zeigen auf dieselbe Neon-Datenbank.
- Zwei wirklich getrennte Neon-Projekte erstellen und `REPLAY_DATABASE_URL` korrigieren.

### N bleibt gleich

- In der App den Bereich **Why does N increase or stay unchanged?** öffnen.
- Provisional, reconstructed, late/post-peak und missing zählen bewusst nicht.

### Mehrfaches Refreshen

- Ein Klick wartet auf alle parallelen Provider und schreibt direkt nach Neon.
- Die Abschlussmeldung nennt ausgefallene Provider. Nur bei einem konkreten Fehler später
  nochmals klicken; blindes Mehrfachklicken ist nicht nötig.

## 15. Was diese Version bewusst nicht ändert

- Madrid-Anchor;
- Champion-Gewichte und feste Biases;
- Regime-Koeffizienten;
- TAF als getrennte Stufe;
- Day-/Peak-Lock-Formel;
- Promotion-Gates und 30 echte sequenzielle OOS-Tage;
- Wett- und Edge-Logik (`RESEARCH ONLY`);
- keine automatische Regeländerung aus dem Replay.
