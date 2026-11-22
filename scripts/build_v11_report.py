"""Render reproducible v11 tables, case registry and calibration plots."""
from collections import Counter
from datetime import date, timedelta
import gzip
import json
from pathlib import Path

from weatherman.v11.evidence import FEATURES, read_rows


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "research/v11"
ART = BASE / "artifacts"


def table(items):
    lines = ["| Kandidat | N | Top-1 | Brier ↓ | Log-Loss ↓ | MAE °C ↓ | 80%-Coverage |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for r in items:
        lines.append(f"| {r['candidate']} | {r['n']} | {r['top1']:.1%} | {r['brier']:.4f} | {r['log_loss']:.4f} | {r['mae']:.3f} | {r['coverage80']:.1%} |")
    return "\n".join(lines)


def main():
    rows = read_rows(ART / "rows.jsonl.gz")
    result = json.loads((ART / "results.json").read_text())
    paired = read_rows(ART / result["paired_artifact"])
    extended = json.loads((ART / "extended_results.json").read_text())
    market = json.loads((ART / "market_proxy.json").read_text())
    source_cases = json.loads((BASE / "madrid_cases_source.json").read_text())
    registry = {x["date"]: x for x in source_cases["cases"]}
    cases = []
    current = date(2026, 10, 11)
    while current <= date(2026, 11, 20):
        day = current.isoformat()
        existing = registry.get(day)
        subset = [r for r in rows if r["target_date"] == day and r["cohort"] == "daily"]
        cases.append({"date": day, "handoff_summary": existing,
                      "summary_available": existing is not None and existing.get("status") == "evaluated",
                      "raw_export_checkpoints": {r["checkpoint"]: r["evidence_class"] for r in subset},
                      "late_observed_max": next((r["observed_max"] for r in subset if "Late" in r["checkpoint"]), None),
                      "stored_metar_actual": subset[0]["actual"] if subset else None,
                      "mandatory_counterexample": day == "2026-11-17"})
        current += timedelta(days=1)
    (ART / "case_registry.json").write_text(json.dumps({
        "version": "v11-madrid-case-registry-1", "cases": cases,
        "source": "Original case summaries through 17 November from PR31 data only; no code dependency. Daily raw exports through 20 November.",
        "limitations": "A summary is not a raw feature/probability vector. Missing original handoffs remain explicitly missing; no invented meteorological account."}, indent=2) + "\n")
    inventory = json.loads((ART / "input_manifest.json").read_text())
    lines = ["# V11 Datenmanifest", "", "Stand: 22.11.2026. Offline, research_only=true, automatic_promotion=false, automatic_execution=false.", "",
             "## Tatsächlich ausgewertete Daten", "",
             "| Bestand | Tage / Zeilen | Evidenz | Verwendung |", "|---|---:|---|---|",
             "| Deep Seasonal Replay, 01.11.2024–08.10.2026 | 685 akzeptierte Tage / 2.740 Checkpoints | historical_reduced_fidelity | langes Discovery-/Kalibrierungs-/Validation-Experiment |",
             "| Historical Replay v0.3, April–Oktober 2026 | 764 ursprüngliche Checkpoints, 735 ursprünglich evaluierbar | Initialisierung erhalten, Veröffentlichung geschätzt | separater Acht-Modell-Vergleich |",
             f"| Daily-Exporte, 11.10.–20.11.2026 | {sum(r['cohort']=='daily' for r in rows)} Checkpoints / 41 Kalendertage | zeitgerecht, rekonstruiert, unbekannt getrennt | retrospektive Diagnose; kein v11-OOS |",
             "| Six-Airport Benchmark | 6 Airports, fertige historische Studien | separate Benchmark-Artefakte | Kontextinventar; keine neu gefittete Multi-Airport-Engine |", "",
             f"Kompakte Hauptkohorte: {len(rows)} Zeilen. Originale Artefakte bleiben unverändert. Quellen-SHA-256: `artifacts/input_manifest.json`; Modell-/Variablenabdeckung: `artifacts/extended_manifest.json`.", "",
             "## Hauptkohorte nach Evidenzklasse", "", "| Klasse | Checkpoints |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(Counter(r["evidence_class"] for r in rows).items())]
    lines += ["", "## Modellabdeckung in der Hauptkohorte", "", "| Modell | Checkpoints | Erster Tag | Letzter Tag |", "|---|---:|---|---|"]
    lines += [f"| {m} | {v['rows']} | {v['first']} | {v['last']} |" for m, v in inventory["models"].items()]
    lines += ["", "Die Zahlen beschreiben verfügbare Datensätze, keine unabhängigen Ensemble-Mitglieder. AROME und AROME HD werden zusätzlich im separaten Experiment geprüft. Meteoblue hat keinen langen historischen Trainingsbestand; kein fairer historischer Skill-Nachweis dafür möglich.", "",
              "## Verfügbarkeit und zeitliche Grenzen", "",
              "Deep: feste Previous-Run-Leads, keine zertifizierten Einzelrun-Publikationszeiten. METAR-Verfügbarkeit: historische Schätzung Beobachtung +5 Minuten. Kein `historical_exact_run`- oder Produktions-OOS-Nachweis.", "",
              "Daily: unabhängige Modellinputs erfordern initialisiert ≤ veröffentlicht ≤ abgerufen ≤ Cutoff, höchstens 48 Stunden seit Initialisierung. Deshalb können sie älter sein als die vom ursprünglichen Collector nach dem Cutoff abgerufenen Inputs. Dieser Versorgungseffekt wird nicht als Prognosefehler des Wettermodells ausgegeben. Modellalter im Featurevektor meint Minuten seit Verfügbarkeit, nicht seit Initialisierung.", "",
              "Daily-Observationen haben Beobachtungszeit, aber keinen verifizierten historischen Empfangszeitpunkt. Der Vergleich bleibt diagnostisch, auch bei ursprünglich scheduled-causal gespeicherten Champion-Forecasts. Diese Klasse zertifiziert nicht den nachträglich gerechneten v11-Kandidaten.", "",
              "Nur final akzeptierte Stored-METAR-Tagesmaxima sind Trainingslabels. AEMET physical Tmax und offizielle WU-Marktauflösung werden nicht als Ersatz verwendet. Labelverfügbarkeit ist historisch Tagesende +10 Minuten geschätzt und wird vor Fit geprüft.", "",
              "## Featureabdeckung", "", "| Feature | Deep nicht-null | Daily nicht-null |", "|---|---:|---:|"]
    for f in FEATURES:
        counts = [sum(r["cohort"] == cohort and r["features"].get(f) is not None for r in rows) for cohort in ("deep", "daily")]
        lines.append(f"| {f} | {counts[0]} | {counts[1]} |")
    lines += ["", "Fehlende Merkmale werden ausschließlich mit Discovery-Medianen und Missingness-Indikatoren behandelt. Nicht verfügbar: durchgängig gemessene Strahlung, vollständige vertikale Profile und historisch verifizierte vollständige Ensemble-Mitglieder. Modellstrahlung ist keine Messung.", "",
              "## Historische APIs und externe Referenzen", "",
              "Am 22.11.2026 wurden die offiziellen Dokumentationen und alle fünf im Handoff genannten öffentlichen Projektseiten geprüft. README-Selbstauskünfte sind keine unabhängig geprüften Erfolgsnachweise; externer Code wurde nicht übernommen.", "",
              "- [Open-Meteo Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api): gestitchte Zeitreihe von Anfangsstunden. Single Runs bewahrt einzelne Läufe; laut Dokumentation ECMWF IFS HRES ab März 2024, andere Modelle ab 02.04.2026. Initialisierung allein belegt keine damalige Veröffentlichungszeit.",
              "- [Open-Meteo Ensemble API](https://open-meteo.com/en/docs/ensemble-api): echte Mitgliederprodukte und Past-Days-Funktion; kein Nachweis eines lückenlosen mehrjährigen Mitgliederarchivs im gelieferten Datenbestand. Keine erfundenen 100 unabhängigen Mitglieder aus sechs deterministischen Modellen.",
              "- [Weather Market Predictor](https://github.com/portrara/weather-market-predictor): probabilistische Priors, Stationsfehler und Spread prüfen; Monte-Carlo-Samples schaffen keine zusätzliche unabhängige Evidenz.",
              "- [Command Center](https://github.com/testedmedia/polymarket-weather-command-center): laufendes Maximum, Hold-Raten und exakte Resolution prüfen; keine pauschalen Solar-/Uhrzeit-Locks übernehmen.",
              "- [Weather Edge](https://github.com/jattree/weather-edge): negativer Postmortem unterstreicht Marktbenchmark und Ausführungskosten; kein Erfolg allein aus Wetter-Kalibrierung ableiten.",
              "- [Augustforge](https://github.com/Augustforge/weather-prediction-market-bot): getrennte Komponenten und unveränderliche Shadow-Aufnahmen als Architekturhinweise, kein nachgewiesener Echtgeld-Edge.",
              "- [Weather Trading Bot](https://github.com/lukasjagnesak/Weather-Trading-Bot): EMOS und Kosten-/Risikologik als Referenzen; keine veröffentlichten Performancebehauptungen als Freigabenachweis.", "",
              "## Bewusst offene Datenlücken", "",
              "Keine neuen öffentlichen API-Backfills ausgelöst; keine Produktions-DB gelesen. Die nachgelagerte Versorgung / Mitgliederarchivierung ist nicht deployt. Nicht vorhandene Original-Handoffs, insbesondere 18.–20.11., werden durch Raw-Exporte ergänzt, aber nicht als gelesene Originaltexte dargestellt. Der 23.10. hatte ursprünglich keinen Abschluss-Handoff; jetzt vorhandene Rohdaten ändern diesen historischen Dokumentationsstatus nicht."]
    (ROOT / "V11_DATA_MANIFEST.md").write_text("\n".join(lines) + "\n")
    lines = ["# V11 Model Bake-off", "", "## Entscheidung", "", "**NO RELEASE CANDIDATE.** Ein Late-Live-Prototyp ist historisch aussichtsreich. First Live braucht eine andere Modellfamilie. Weder echte v11-OOS-Evidenz noch ein ausführbarer Trading-Edge sind nachgewiesen.", "",
             "`research_only=true`, `automatic_promotion=false`, `automatic_execution=false`.", "",
             "## Experiment und Auswahl", "",
             "Hauptstudie LEMD: Discovery 01.11.2024–31.08.2025; separate Kalibrierung/Modellauswahl 01.09.–31.10.2025; historische Validation 01.11.2025–08.10.2026. Zusätzliche Daily-Diagnose 11.10.–20.11.2026. Die Zeitblöcke sind getrennt, aber diese historische Untersuchung ist retrospektiv; frühere Studien haben Teile dieser Zeiträume bereits betrachtet. Kein unberührter prospektiver Holdout wird behauptet.", "",
             "Einzelmodelle, Gleichgewichtung, ursprüngliche Champion-Gewichte, inverse MAE, Brier-/Log-Score-Gewichte, Saison-/Checkpoint-/Regime-/Freshness-Varianten, empirische Residualverteilungen, EMOS und Spread-Inflation. Parametrisierung und Auswahlliste stehen in `artifacts/model_config.json`.", "",
             "Direktmodell: regulierte multinomiale logistische Regression für Delta 0/+1/≥2. Verteilung innerhalb ≥2 aus früheren empirischen Delta-Häufigkeiten mit Pseudocounts. Diese einfache Tail-Verteilung ist bei großen Restanstiegen zu grob. Full-, ohne Wind/Feuchte- und ohne Strahlungsmerkmale als separate Ablationen. Sie unterscheiden Merkmalsgruppen, beweisen keine physikalischen Erwärmungsursachen.", "",
             "Die Namen `delta_solar` / `delta_no_solar` sind Merkmalsablationen. Insbesondere `delta_solar` verwendet weiter Saison, Modellrestanstieg und Temperaturverlauf; es ist weder ein physikalisches Solarmodell noch eine Nachtabschaltung.", "",
             "`observed_floor` bildet max(beobachtetes Maximum, künftige Temperatur) ab: Untere Masse landet am beobachteten Bucket. Kein Abschneiden mit anschließender künstlicher Erhöhung der oberen Wahrscheinlichkeiten. Ein bereits beobachtetes Maximum wird als harte untere Grenze, nicht als obere Sperre verwendet.", "",
             "Der Normal-Location-Parameter ist eine statistische kontinuierliche Approximation des ganzzahlig gemeldeten METAR-Targets. Eine physikalische subgradgenaue latente Temperatur ist mit diesen ganzzahligen Labels nicht identifizierbar und wird nicht als solche ausgegeben. Explizite Rundungs- und Marktgrenzenfunktionen sind separat vorhanden.", "",
             "Gefittete Kandidatenauswahl ausschließlich anhand des vorangegangenen Kalibrierungsblocks:", "", "| Checkpoint | Ausgewählt |", "|---|---|"]
    lines += [f"| {cp} | {n} |" for cp, n in result["frozen_selection"].items()]
    lines += ["", "## Historische Validation: gleiche Tage je Tabelle", ""]
    for cp in result["frozen_selection"]:
        names = {"champion", result["frozen_selection"][cp], "equal_raw", "delta_full", "hour_hold_rate", "observed_max"}
        subset = [r for r in result["summaries"] if r["phase"] == "historical_validation" and r["checkpoint"] == cp and r["candidate"] in names]
        pair = next(r for r in paired if r["phase"] == "historical_validation" and r["checkpoint"] == cp and r["candidate"] == result["frozen_selection"][cp] and r["reference"] == "champion")
        subset = [{"candidate": "champion", **pair["reference_metrics"]} if r["candidate"] == "champion" else r for r in subset]
        lines += ["### " + cp, "", table(subset), ""]
    lines += ["First Live: Direkt-Delta verworfen. Sein ≥2-Tail differenziert die große verbleibende Erwärmung unzureichend. Der EMOS-Prior mit Maximum-Floor verbessert diese historische Vergleichskohorte moderat, ist aber kein Produktionskandidat.", "",
              "Late: der vorab gewählte Direktansatz verbessert Champion in dieser reduzierten historischen Kohorte. Die Full-Variante sieht auf einigen Validation-Metriken etwas besser aus; sie wird deshalb nicht nachträglich zum eingefrorenen Gewinner erklärt. Neue Auswahl erfordert eine neue Version und frische Evidenz.", "",
              "## Daily-Diagnose", ""]
    for cp in ("First Live @12:00", "Late Live @16:00"):
        names = {"champion", result["frozen_selection"][cp], "delta_full", "observed_max"}
        part = [r for r in result["summaries"] if r["phase"] == "daily_diagnostic" and r["evidence_class"] == "scheduled_causal_production" and r["checkpoint"] == cp and r["candidate"] in names]
        lines += ["### " + cp + " — ursprünglich zeitgerechte Champion-Aufnahmen", "", table(part), ""]
    legacy = [r for r in rows if r["cohort"] == "daily" and "Late" in r["checkpoint"] and r["target_date"] >= "2026-10-24" and r["evidence_class"] == "scheduled_causal_production"]
    lines += [f"Kontrollrechnung des alten Prüfberichts: beobachtetes Maximum {sum(r['actual']==r['observed_max'] for r in legacy)}/{len(legacy)} Treffer am Late-Checkpoint 24.10.–20.11.; rekonstruierten 16.11. separat halten. Die erweiterten 40 Tage dürfen nicht mit der ursprünglichen 27-Tage-Kohorte verwechselt werden.", "",
              "## Getrennter Acht-Modell-Vergleich", "",
              "Zusätzlich wurden AROME und AROME HD auf einem separaten gemeinsamen Acht-Modell-Sample untersucht: Discovery April–Juni 2026, Kalibrierungsblock Juli, Validation August–08.10.2026. Rohmodelle und ausschließlich vorher gelernte modellbezogene Bias-/Residualverteilungen. Meteoblue mangels längerem historischem Training ausgeschlossen. Vollständige Tabellen: `artifacts/extended_results.json`.", ""]
    for cp in extended["frozen_models"]:
        part = [r for r in extended["summaries"] if r["phase"] == "historical_validation" and r["checkpoint"] == cp and r["candidate"] in {"champion", "meteofrance_arome_france", "meteofrance_arome_france_hd", "best_training_single", "equal:residual", "inverse_mae:residual"}]
        lines += ["### " + cp, "", table(part), ""]
    lines += ["Ein höherer Top-1-Wert eines Einzelmodells beweist keine bessere Wahrscheinlichkeitsverteilung. In diesem Zusatzsample gewinnt keine getestete einfache Einzelmodell-/Mittelwertvariante beim Log-Loss gegen den Replay-Champion. Diese Kohorte nicht mit der langen reduzierten Kohorte zusammenfassen.", "",
              "## Kalibrierung und Unsicherheit", "",
              "Brier ist der unskalierte Mehrklassen-Brier, Log-Loss nutzt einen expliziten 1e-12-Boden und zählt Nullmasse-Fälle zusätzlich. CRPS ist hier der diskrete RPS auf ganzzahligen, gleich weit entfernten Temperatur-Buckets. Top-Label-ECE ist keine vollständige multivariate Kalibrierungsprüfung. 80%-Intervalle sind diskrete Quantilintervalle und können konservativ überdecken. Sharpness steht als Intervallbreite in den maschinenlesbaren Ergebnissen.", "",
              "Die Coverage liegt teils deutlich über 80%. Das ist kein Nachweis, dass individuelle 80%-Zahlen korrekt sind. Alle v11-Ausgaben bleiben `provisional`. Temperatur-Skalierung wurde ausschließlich im separaten früheren Kalibrierungsblock angepasst; sie behebt keine falsche Modal-Reihenfolge.", "",
              "`artifacts/results.json` enthält paarweise Vergleiche auf identischen IDs, Saison-/Regime-Slices, Fehler bei späteren Touches und fälschlich erwartete weitere Buckets. Die beigefügten 500-fachen IID-Tages-Bootstrapintervalle sind deskriptiv; zeitliche Abhängigkeit und Mehrfachtests sind nicht korrigiert. Keine Freigabe allein daraus ableiten.", "",
              "## Marktvergleich", "",
              "Historische vollständige und zeitlich passende Preisreihen liefern nur ein kleines Sample. Normierung der gegenseitig ausschließenden Serienpreise ist eine Markt-Proxyverteilung, kein Ask und kein Orderbuch. Stored-METAR- und gemeldete offizielle WU-Resolution werden parallel geprüft, nicht gleichgesetzt.", "",
              "| Checkpoint | N | Wetter Log-Loss | Markt-Proxy Log-Loss |", "|---|---:|---:|---:|"]
    for cp in result["frozen_selection"]:
        part = [r for r in market["summaries"] if r["checkpoint"] == cp and r["target"] == "stored_metar_proxy"]
        if part:
            d = {r["candidate"]: r for r in part}
            weather, price = d["weather_frozen_selection"], d["normalized_market_series"]
            lines.append(f"| {cp} | {weather['n']} | {weather['log_loss']:.4f} | {price['log_loss']:.4f} |")
    lines += ["", "Im kleinen Late-Proxy-Sample ist die Marktverteilung besser. Kein belastbarer Nachweis, dass v11 den Markt schlägt. Die Kategorien von Stored METAR und der berichteten offiziellen Resolution stimmen in den 47 ausgewerteten Fällen überein; das beweist keine generelle Gleichheit der Zielgrößen.", "",
              "Best Ask, historische Spreads, Gebühren, Tiefe und tatsächliche Fills fehlen. **Trading-Edge nicht nachgewiesen; offizieller ROI = nicht berechenbar.** `market_proxy.json` dokumentiert alle Ausschlüsse. Forecast-Freeze und spätere getrennte Marktprüfung sind als lokale, unveränderliche Shadow-Bausteine implementiert; sie lösen keine Orders aus.", "",
              "## Pflichtfälle und verbleibende Komponenten", "",
              "Alle Kalendertage 11.10.–20.11. stehen in `case_registry.json`, einschließlich fehlender Original-Handoffs. Der 17.11. bleibt Pflichtgegenfall: beobachtetes Maximum 16 am Cutoff, späteres Maximum 17; kein harter Solar-/Abend-Lock. Der Test verlangt nicht, den bereits bekannten Tag künstlich perfekt zu treffen, sondern prüft Cutoff und positive obere Masse.", "",
              "Das diskrete Hazard-Aggregat ist als isolierte, getestete Funktion vorbereitet. Ein empirisch gefittetes 30-Minuten-Hazard-Modell ist noch nicht vorhanden und wird nicht als evaluiert ausgegeben. Ebenso fehlen eine empirisch identifizierte Mischung physikalischer Erwärmungspfade und ein vollständiges Mitgliederensemble. Nicht alle Architekturideen aus dem Handoff sind damit schon wissenschaftlich validiert.", "",
              "## Freigabestatus und nächste Entscheidung", "",
              "1. Kein gemeinsamer v11-Allzweckkandidat: First und Late getrennt fortführen.",
              "2. Für Late ist direkte Restanstiegswahrscheinlichkeit aussichtsreicher als weitere additive °C-Patches; erst Versorgung/Provenienz und Prospektivaufnahme absichern.",
              "3. Für First EMOS mit beobachtetem Maximum prüfen; Direkt-Delta in dieser Version nicht zum OOS zulassen.",
              "4. Zuerst exakte künftige Modell-/Observationsempfänge und Mitglieder archivieren; keine nachträgliche Rekonstruktion als OOS zählen.",
              "5. Vor einem zukünftigen OOS-Start Modell/Parameter/Featureversion einfrieren und das vorab definierte Protokoll in `research/v11/OOS_GATES.json` anwenden. Mindestens 30 echte sequenzielle Tage sind nur die Daueruntergrenze; für die Kalibrierung werden zusätzlich mindestens 100 Tage und ausreichend besetzte Reliability-Bins gefordert.",
              "6. Markt-/Trading-Gate bleibt mangels ausführbarer historischer Quotes blockiert. Eigenständige Preisarchivierung und offizielle Resolution sind Voraussetzung.", "",
              "PR31 bleibt getrennt; keine Runtime-Abhängigkeit. Champion, geschützte Baseline, UI, Gewichte, produktive Kalibrierung aus PR30, Datenbank und Scheduler sind unverändert. Kein Merge und kein Deployment.", "",
              "## Reproduktion", "", "```bash", "python -m venv .venv", ".venv/bin/pip install -e '.[dev]' -r research/v11/requirements.txt",
              ".venv/bin/python scripts/run_v11_research.py --rows research/v11/artifacts/rows.jsonl.gz --output /tmp/v11-replay",
              ".venv/bin/python scripts/run_v11_extended_models.py --rows research/v11/artifacts/extended_rows.jsonl.gz --output /tmp/v11-extended",
              ".venv/bin/pytest tests/test_v11_research.py -q", "```", "",
              "Neu-Normalisierung: `run_v11_research.py --deep UNPACKED_DEEP --daily RELEASE_REVIEW/inputs --output OUTPUT --normalize-only`. Marktproxy: `run_v11_market_proxy.py --prices DEEP/markets/checkpoint-price-series.json --artifacts ARTIFACTS`. Rohpreisreihen selbst liegen im ursprünglichen ZIP; kompakte Wetterinputs und Ergebnisse sind im Branch enthalten. Exakte Rohquellen-/Archiv-Hashes stehen im Manifest. Modelle werden ausschließlich lokal aus bekannten eigenen joblib-Dateien geladen; fremde Pickles nicht öffnen.", ""]
    (ROOT / "V11_MODEL_BAKEOFF.md").write_text("\n".join(lines))
    # Standalone scientific figure, not generated imagery.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for ax, cp in zip(axes, ("First Live @12:00", "Late Live @16:00")):
        ax.plot([0, 1], [0, 1], "--", color="gray", linewidth=1)
        for name in ("champion", result["frozen_selection"][cp]):
            r = next(r for r in result["summaries"] if r["phase"] == "historical_validation" and r["checkpoint"] == cp and r["candidate"] == name)
            bins = [b for b in r["reliability"] if b["n"] >= 10]
            ax.plot([b["predicted"] for b in bins], [b["observed"] for b in bins], "o-", label=name)
            for b in bins:
                ax.annotate(str(b["n"]), (b["predicted"], b["observed"]), xytext=(3, 4), textcoords="offset points", fontsize=7)
        ax.set(xlim=(0, 1), ylim=(0, 1), title=cp, xlabel="Mean predicted top-bucket probability", ylabel="Observed top-bucket hit rate")
        ax.legend(fontsize=8)
        ax.grid(alpha=.2)
    fig.suptitle("Historical reduced-fidelity validation — provisional\nCounts shown; bins with n < 10 omitted")
    fig.tight_layout()
    fig.savefig(ART / "reliability.png", dpi=170)
    plt.close(fig)
    # Preserve all per-day scores in a bounded portable artifact.
    path = ART / "scores.csv"
    if path.exists():
        (ART / "scores.csv.gz").write_bytes(gzip.compress(path.read_bytes(), mtime=0))


if __name__ == "__main__":
    main()
