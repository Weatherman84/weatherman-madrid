# V11 Model Bake-off

## Entscheidung

**NO RELEASE CANDIDATE.** Ein Late-Live-Prototyp ist historisch aussichtsreich. First Live braucht eine andere Modellfamilie. Weder echte v11-OOS-Evidenz noch ein ausführbarer Trading-Edge sind nachgewiesen.

`research_only=true`, `automatic_promotion=false`, `automatic_execution=false`.

## Experiment und Auswahl

Hauptstudie LEMD: Discovery 01.11.2024–31.08.2025; separate Kalibrierung/Modellauswahl 01.09.–31.10.2025; historische Validation 01.11.2025–08.10.2026. Zusätzliche Daily-Diagnose 11.10.–20.11.2026. Die Zeitblöcke sind getrennt, aber diese historische Untersuchung ist retrospektiv; frühere Studien haben Teile dieser Zeiträume bereits betrachtet. Kein unberührter prospektiver Holdout wird behauptet.

Einzelmodelle, Gleichgewichtung, ursprüngliche Champion-Gewichte, inverse MAE, Brier-/Log-Score-Gewichte, Saison-/Checkpoint-/Regime-/Freshness-Varianten, empirische Residualverteilungen, EMOS und Spread-Inflation. Parametrisierung und Auswahlliste stehen in `artifacts/model_config.json`.

Direktmodell: regulierte multinomiale logistische Regression für Delta 0/+1/≥2. Verteilung innerhalb ≥2 aus früheren empirischen Delta-Häufigkeiten mit Pseudocounts. Diese einfache Tail-Verteilung ist bei großen Restanstiegen zu grob. Full-, ohne Wind/Feuchte- und ohne Strahlungsmerkmale als separate Ablationen. Sie unterscheiden Merkmalsgruppen, beweisen keine physikalischen Erwärmungsursachen.

Die Namen `delta_solar` / `delta_no_solar` sind Merkmalsablationen. Insbesondere `delta_solar` verwendet weiter Saison, Modellrestanstieg und Temperaturverlauf; es ist weder ein physikalisches Solarmodell noch eine Nachtabschaltung.

`observed_floor` bildet max(beobachtetes Maximum, künftige Temperatur) ab: Untere Masse landet am beobachteten Bucket. Kein Abschneiden mit anschließender künstlicher Erhöhung der oberen Wahrscheinlichkeiten. Ein bereits beobachtetes Maximum wird als harte untere Grenze, nicht als obere Sperre verwendet.

Der Normal-Location-Parameter ist eine statistische kontinuierliche Approximation des ganzzahlig gemeldeten METAR-Targets. Eine physikalische subgradgenaue latente Temperatur ist mit diesen ganzzahligen Labels nicht identifizierbar und wird nicht als solche ausgegeben. Explizite Rundungs- und Marktgrenzenfunktionen sind separat vorhanden.

Gefittete Kandidatenauswahl ausschließlich anhand des vorangegangenen Kalibrierungsblocks:

| Checkpoint | Ausgewählt |
|---|---|
| D-1 Evening @20:00 | emos_spread:1.0 |
| D0 Morning @09:00 | emos_spread:1.0 |
| First Live @12:00 | emos_spread:1.0:floor |
| Late Live @16:00 | delta_solar |

## Historische Validation: gleiche Tage je Tabelle

### D-1 Evening @20:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| champion | 329 | 44.1% | 0.6868 | 1.5220 | 0.791 | 89.1% |
| emos_spread:1.0 | 329 | 40.7% | 0.7007 | 1.4696 | 0.807 | 90.6% |
| equal_raw | 329 | 36.2% | 0.7623 | 1.7669 | 0.872 | 84.8% |

### D0 Morning @09:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| champion | 333 | 41.7% | 0.7068 | 1.5115 | 0.826 | 86.2% |
| emos_spread:1.0 | 333 | 47.4% | 0.6686 | 1.3654 | 0.751 | 91.9% |
| equal_raw | 333 | 39.0% | 0.7395 | 1.6644 | 0.837 | 82.9% |

### First Live @12:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| champion | 333 | 43.5% | 0.6838 | 1.4453 | 0.772 | 90.4% |
| delta_full | 333 | 20.7% | 0.8812 | 2.2120 | 1.935 | 90.1% |
| emos_spread:1.0:floor | 333 | 48.3% | 0.6645 | 1.3558 | 0.749 | 91.9% |
| equal_raw | 333 | 39.0% | 0.7395 | 1.6644 | 0.837 | 82.9% |
| hour_hold_rate | 333 | 15.9% | 0.8988 | 2.3685 | 2.311 | 84.7% |
| observed_max | 333 | 3.3% | 1.9339 | 26.7183 | 5.285 | 3.3% |

### Late Live @16:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| champion | 333 | 48.9% | 0.5959 | 1.0631 | 0.637 | 95.8% |
| delta_full | 333 | 61.6% | 0.5094 | 0.9015 | 0.470 | 96.7% |
| delta_solar | 333 | 59.5% | 0.5106 | 0.9109 | 0.481 | 96.1% |
| equal_raw | 333 | 39.0% | 0.7395 | 1.6644 | 0.837 | 82.9% |
| hour_hold_rate | 333 | 41.1% | 0.6647 | 1.2025 | 0.712 | 96.4% |
| observed_max | 333 | 41.1% | 1.1772 | 16.2633 | 0.859 | 41.1% |

First Live: Direkt-Delta verworfen. Sein ≥2-Tail differenziert die große verbleibende Erwärmung unzureichend. Der EMOS-Prior mit Maximum-Floor verbessert diese historische Vergleichskohorte moderat, ist aber kein Produktionskandidat.

Late: der vorab gewählte Direktansatz verbessert Champion in dieser reduzierten historischen Kohorte. Die Full-Variante sieht auf einigen Validation-Metriken etwas besser aus; sie wird deshalb nicht nachträglich zum eingefrorenen Gewinner erklärt. Neue Auswahl erfordert eine neue Version und frische Evidenz.

## Daily-Diagnose

### First Live @12:00 — ursprünglich zeitgerechte Champion-Aufnahmen

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| champion | 41 | 41.5% | 0.6960 | 1.4390 | 0.821 | 87.8% |
| delta_full | 41 | 9.8% | 1.0263 | 2.5097 | 1.261 | 85.4% |
| emos_spread:1.0:floor | 41 | 39.0% | 0.6938 | 1.3031 | 0.758 | 92.7% |
| observed_max | 41 | 14.6% | 1.7073 | 23.5875 | 3.951 | 14.6% |

### Late Live @16:00 — ursprünglich zeitgerechte Champion-Aufnahmen

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| champion | 40 | 45.0% | 0.6540 | 1.1549 | 0.704 | 95.0% |
| delta_full | 40 | 72.5% | 0.3694 | 0.6331 | 0.367 | 97.5% |
| delta_solar | 40 | 77.5% | 0.3640 | 0.6377 | 0.387 | 95.0% |
| observed_max | 40 | 62.5% | 0.7500 | 10.3616 | 0.425 | 62.5% |

Kontrollrechnung des alten Prüfberichts: beobachtetes Maximum 21/27 Treffer am Late-Checkpoint 24.10.–20.11.; rekonstruierten 16.11. separat halten. Die erweiterten 40 Tage dürfen nicht mit der ursprünglichen 27-Tage-Kohorte verwechselt werden.

## Getrennter Acht-Modell-Vergleich

Zusätzlich wurden AROME und AROME HD auf einem separaten gemeinsamen Acht-Modell-Sample untersucht: Discovery April–Juni 2026, Kalibrierungsblock Juli, Validation August–08.10.2026. Rohmodelle und ausschließlich vorher gelernte modellbezogene Bias-/Residualverteilungen. Meteoblue mangels längerem historischem Training ausgeschlossen. Vollständige Tabellen: `artifacts/extended_results.json`.

### D-1 Evening @20:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| best_training_single | 68 | 45.6% | 0.6942 | 1.7445 | 0.831 | 92.6% |
| champion | 68 | 50.0% | 0.6235 | 1.1971 | 0.687 | 95.6% |
| equal:residual | 68 | 35.3% | 0.6963 | 1.4360 | 0.773 | 92.6% |
| inverse_mae:residual | 68 | 38.2% | 0.6923 | 1.4392 | 0.770 | 94.1% |
| meteofrance_arome_france | 68 | 38.2% | 0.7479 | 1.5782 | 0.854 | 91.2% |
| meteofrance_arome_france_hd | 68 | 36.8% | 0.7542 | 1.6037 | 0.896 | 91.2% |

### D0 Morning @09:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| best_training_single | 67 | 52.2% | 0.6639 | 1.3921 | 0.679 | 94.0% |
| champion | 67 | 46.3% | 0.6547 | 1.2456 | 0.688 | 95.5% |
| equal:residual | 67 | 50.7% | 0.6599 | 1.3123 | 0.657 | 97.0% |
| inverse_mae:residual | 67 | 53.7% | 0.6593 | 1.3110 | 0.657 | 97.0% |
| meteofrance_arome_france | 67 | 44.8% | 0.7042 | 1.4159 | 0.712 | 97.0% |
| meteofrance_arome_france_hd | 67 | 40.3% | 0.7107 | 1.4300 | 0.745 | 95.5% |

### First Live @12:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| best_training_single | 68 | 51.5% | 0.6586 | 1.4240 | 0.664 | 91.2% |
| champion | 68 | 51.5% | 0.6269 | 1.2529 | 0.592 | 95.6% |
| equal:residual | 68 | 50.0% | 0.6609 | 1.3268 | 0.668 | 97.1% |
| inverse_mae:residual | 68 | 51.5% | 0.6610 | 1.3265 | 0.670 | 97.1% |
| meteofrance_arome_france | 68 | 47.1% | 0.7037 | 1.4253 | 0.735 | 92.6% |
| meteofrance_arome_france_hd | 68 | 44.1% | 0.7115 | 1.4476 | 0.761 | 92.6% |

### Late Live @16:00

| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |
|---|---:|---:|---:|---:|---:|---:|
| best_training_single | 68 | 51.5% | 0.6703 | 1.4277 | 0.704 | 89.7% |
| champion | 68 | 50.0% | 0.5882 | 1.0517 | 0.565 | 98.5% |
| equal:residual | 68 | 45.6% | 0.6690 | 1.3057 | 0.690 | 94.1% |
| inverse_mae:residual | 68 | 44.1% | 0.6704 | 1.3135 | 0.696 | 94.1% |
| meteofrance_arome_france | 68 | 44.1% | 0.6819 | 1.3259 | 0.685 | 95.6% |
| meteofrance_arome_france_hd | 68 | 44.1% | 0.6900 | 1.3439 | 0.707 | 94.1% |

Ein höherer Top-1-Wert eines Einzelmodells beweist keine bessere Wahrscheinlichkeitsverteilung. In diesem Zusatzsample gewinnt keine getestete einfache Einzelmodell-/Mittelwertvariante beim Log-Loss gegen den Replay-Champion. Diese Kohorte nicht mit der langen reduzierten Kohorte zusammenfassen.

## Kalibrierung und Unsicherheit

Brier ist der unskalierte Mehrklassen-Brier, Log-Loss nutzt einen expliziten 1e-12-Boden und zählt Nullmasse-Fälle zusätzlich. Für Top-1/Top-2 werden Wahrscheinlichkeiten beim Ranking auf zwölf Dezimalstellen gerundet; Gleichstände gewinnt der niedrigere Bucket. Das verhindert plattformabhängige Reihenfolgen symmetrischer CDF-Differenzen, ohne die Verteilungen oder Proper Scores zu runden. CRPS ist hier der diskrete RPS auf ganzzahligen, gleich weit entfernten Temperatur-Buckets. Top-Label-ECE ist keine vollständige multivariate Kalibrierungsprüfung. 80%-Intervalle sind diskrete Quantilintervalle und können konservativ überdecken. Sharpness steht als Intervallbreite in den maschinenlesbaren Ergebnissen.

Die Coverage liegt teils deutlich über 80%. Das ist kein Nachweis, dass individuelle 80%-Zahlen korrekt sind. Alle v11-Ausgaben bleiben `provisional`. Temperatur-Skalierung wurde ausschließlich im separaten früheren Kalibrierungsblock angepasst; sie behebt keine falsche Modal-Reihenfolge.

`artifacts/results.json` enthält paarweise Vergleiche auf identischen IDs, Saison-/Regime-Slices, Fehler bei späteren Touches und fälschlich erwartete weitere Buckets. Die beigefügten 500-fachen IID-Tages-Bootstrapintervalle sind deskriptiv; zeitliche Abhängigkeit und Mehrfachtests sind nicht korrigiert. Keine Freigabe allein daraus ableiten.

## Marktvergleich

Historische vollständige und zeitlich passende Preisreihen liefern nur ein kleines Sample. Normierung der gegenseitig ausschließenden Serienpreise ist eine Markt-Proxyverteilung, kein Ask und kein Orderbuch. Stored-METAR- und gemeldete offizielle WU-Resolution werden parallel geprüft, nicht gleichgesetzt.

| Checkpoint | N | Wetter Log-Loss | Markt-Proxy Log-Loss |
|---|---:|---:|---:|
| D-1 Evening @20:00 | 13 | 1.5554 | 1.5712 |
| D0 Morning @09:00 | 14 | 1.8392 | 1.3566 |
| First Live @12:00 | 14 | 1.8392 | 1.5164 |
| Late Live @16:00 | 6 | 0.8082 | 0.3986 |

Im kleinen Late-Proxy-Sample ist die Marktverteilung besser. Kein belastbarer Nachweis, dass v11 den Markt schlägt. Die Kategorien von Stored METAR und der berichteten offiziellen Resolution stimmen in den 47 ausgewerteten Fällen überein; das beweist keine generelle Gleichheit der Zielgrößen.

Best Ask, historische Spreads, Gebühren, Tiefe und tatsächliche Fills fehlen. **Trading-Edge nicht nachgewiesen; offizieller ROI = nicht berechenbar.** `market_proxy.json` dokumentiert alle Ausschlüsse. Forecast-Freeze und spätere getrennte Marktprüfung sind als lokale, unveränderliche Shadow-Bausteine implementiert; sie lösen keine Orders aus.

## Pflichtfälle und verbleibende Komponenten

Alle Kalendertage 11.10.–20.11. stehen in `case_registry.json`, einschließlich fehlender Original-Handoffs. Der 17.11. bleibt Pflichtgegenfall: beobachtetes Maximum 16 am Cutoff, späteres Maximum 17; kein harter Solar-/Abend-Lock. Der Test verlangt nicht, den bereits bekannten Tag künstlich perfekt zu treffen, sondern prüft Cutoff und positive obere Masse.

Das diskrete Hazard-Aggregat ist als isolierte, getestete Funktion vorbereitet. Ein empirisch gefittetes 30-Minuten-Hazard-Modell ist noch nicht vorhanden und wird nicht als evaluiert ausgegeben. Ebenso fehlen eine empirisch identifizierte Mischung physikalischer Erwärmungspfade und ein vollständiges Mitgliederensemble. Nicht alle Architekturideen aus dem Handoff sind damit schon wissenschaftlich validiert.

## Freigabestatus und nächste Entscheidung

1. Kein gemeinsamer v11-Allzweckkandidat: First und Late getrennt fortführen.
2. Für Late ist direkte Restanstiegswahrscheinlichkeit aussichtsreicher als weitere additive °C-Patches; erst Versorgung/Provenienz und Prospektivaufnahme absichern.
3. Für First EMOS mit beobachtetem Maximum prüfen; Direkt-Delta in dieser Version nicht zum OOS zulassen.
4. Zuerst exakte künftige Modell-/Observationsempfänge und Mitglieder archivieren; keine nachträgliche Rekonstruktion als OOS zählen.
5. Vor einem zukünftigen OOS-Start Modell/Parameter/Featureversion einfrieren und das vorab definierte Protokoll in `research/v11/OOS_GATES.json` anwenden. Mindestens 30 echte sequenzielle Tage sind nur die Daueruntergrenze; für die Kalibrierung werden zusätzlich mindestens 100 Tage und ausreichend besetzte Reliability-Bins gefordert.
6. Markt-/Trading-Gate bleibt mangels ausführbarer historischer Quotes blockiert. Eigenständige Preisarchivierung und offizielle Resolution sind Voraussetzung.

PR31 bleibt getrennt; keine Runtime-Abhängigkeit. Champion, geschützte Baseline, UI, Gewichte, produktive Kalibrierung aus PR30, Datenbank und Scheduler sind unverändert. Kein Merge und kein Deployment.

## Reproduktion

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]' -r research/v11/requirements.txt
.venv/bin/python scripts/run_v11_research.py --rows research/v11/artifacts/rows.jsonl.gz --output /tmp/v11-replay
.venv/bin/python scripts/run_v11_extended_models.py --rows research/v11/artifacts/extended_rows.jsonl.gz --output /tmp/v11-extended
.venv/bin/pytest tests/test_v11_research.py -q
```

Neu-Normalisierung: `run_v11_research.py --deep UNPACKED_DEEP --daily RELEASE_REVIEW/inputs --output OUTPUT --normalize-only`. Marktproxy: `run_v11_market_proxy.py --prices DEEP/markets/checkpoint-price-series.json --artifacts ARTIFACTS`. Rohpreisreihen selbst liegen im ursprünglichen ZIP; kompakte Wetterinputs und Ergebnisse sind im Branch enthalten. Exakte Rohquellen-/Archiv-Hashes stehen im Manifest. Modelle werden ausschließlich lokal aus bekannten eigenen joblib-Dateien geladen; fremde Pickles nicht öffnen.
