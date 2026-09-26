# Research Watchlist – v1.0.13

Alle Punkte bleiben `RESEARCH ONLY`. Es folgt daraus keine Änderung an Forecast
Engine, Champion, Biases, Clear-Sky Override, TAF-Stufe, Locks oder Promotion.

## Evidenzhygiene aus v1.0.12

- Scheduled-causal, reconstructed-research und all-research getrennt auswerten.
- Rekonstruierte Checkpoints niemals als historische OOS- oder Live-Shadow-Evidenz
  bezeichnen.
- Trading-Decisions führen `checkpoint_status`, `checkpoint_reconstructed` und die
  normalisierte Evidenzklasse.

## 16. September 2026 – echter OOS-Tag 16/30

- Alle vier Fixpunkte waren `scheduled-causal`, `evidence_class="complete"` und nicht
  rekonstruiert. Der Tag gehört vollständig zur OOS-Hauptkohorte.
- D−1, D0 und First Live trafen Stored METAR 28; Late Live wechselte fälschlich auf
  29. First Live Center 28,34 °C; Late Live 29,35 °C und damit +1,35 K zu warm.
- Raw 28,36 und Bias 28,54 lagen deutlich näher; die Abweichung entstand im späten
  Live-/Conditioning-Komplex.
- Positive Late-Live-Beiträge: Clear-Sky Override +0,30 K, Cloud +0,20 K,
  Heating +0,18 K, Radiation +0,148 K und jüngster Stationsfehler +0,093 K.
  Wind wirkte bereits mit −0,133 K. Kein pauschal stärkerer Windabschlag ableiten.
- Modellpeak war passiert, `remaining_model_rise_c` lag bei rund 0,1 K, beobachtetes
  Maximum und TAF TX lagen beide bei 28, während Pre-TAF-Modal und Final-Modal 29
  blieben. TAF-Impact war 0,0 K.
- Analog Memory lag an allen Checkpoints numerisch näher, blieb Late Live aber ebenfalls
  im falschen 29er-Bucket. Keine Promotion.
- AEMET war wegen HTTP 503 nicht verfügbar und darf nicht rekonstruiert werden.

## Neue Research-Prüfung

`late_live_overlap_guard_candidate_v0.1` markiert ausschließlich Fälle mit:

- Late Live;
- `MODEL PEAK PASSED`;
- `remaining_model_rise_c <= 0,2`;
- beobachtetem Maximum gleich TAF-TX;
- TAF-Bucket unter Pre-TAF-Modal;
- positivem Clear-Sky-/Cloud-/Radiation-Overlap.

Der Shadow speichert Komponenten und Provenienz, aber keinen erfundenen Cap, keine
Tail-Dämpfung und keine Wahrscheinlichkeit. `champion_impact_c=0.0`,
`automatic_promotion=false`, Status `insufficient_oos_data`.

## 17. September 2026 – echter OOS-Tag 17/30

- Alle vier Checkpoints waren `scheduled-causal`, vollständig und nicht
  rekonstruiert. Nur Late Live traf Stored METAR 27; die drei frühen Checkpoints
  führten 26.
- Drei aufeinanderfolgende 27-°C-Meldungen von 17:00 bis 18:00 LT bestätigen ein
  Plateau und keinen Einmal-Touch.
- Late Live stieg von Weighted Raw 26,20 über Bias 26,41 auf Champion 27,38 °C.
  Der Uplift aus Temperaturanker, Clear-Sky, Wind, Cloud und kleineren Komponenten
  war diesmal berechtigt und brachte den Forecast in den richtigen Bucket.
- TAF TX28 lag einen Bucket über Actual. Der begrenzte positive Einfluss von rund
  +0,16 bis +0,18 K erzeugte keinen falschen Modal-Flip; Late Live blieb 27 mit
  43,3 % vor 28 mit 35,6 %.
- Für frühe 26er-Verteilungen untersuchen, ob ein zwei Buckets höheres TAF-TX genug
  Wahrscheinlichkeit in den Zwischenbucket legt, ohne das TAF als Zielwert zu setzen.
- Analog Memory war früh näher, blieb aber überwiegend im 26er-Bereich. Airport Anchor
  Transfer war Late Live mit 27,21 °C numerisch am besten. Beide weiter OOS beobachten,
  keine Promotion.
- AEMET war wegen HTTP 503 nicht verfügbar und wird weder rekonstruiert noch als
  Market-Actual verwendet.

## 18. September 2026 – echter OOS-Tag 18/30

- Alle vier Fixpunkte waren `scheduled-causal`, vollständig und nicht rekonstruiert;
  alle vier Modal-Buckets trafen Stored METAR 28.
- Das Maximum bestand aus vier aufeinanderfolgenden 28-°C-Meldungen von 17:30 bis
  19:00 LT. Als bestätigtes Plateau auswerten, nicht als Einmal-Touch.
- Late Live lag bei 27,83 °C. Clear-Sky Override war 0,0 K und der Netto-Live-Effekt
  trotz `Model Ceiling Reached Early` −0,075 K. Die offene Heizperiode führte damit
  nicht zur Überkorrektur des 16. September.
- TAF TX29 erhöhte den Upper Tail moderat, ohne den richtigen Modal-Bucket 28 zu
  verdrängen. Top 1 und Top 2 lagen Late Live nur 1,3 Prozentpunkte auseinander;
  korrekter Treffer mit hoher Bucket-Unsicherheit, nicht High Confidence.
- Der Analog-Challenger war an drei frühen Checkpoints näher, Late Live der Champion.
  Positive Challenger-Evidenz ohne Promotionsgrund.

## 19. September 2026 – echter OOS-Tag 19/30

- Alle vier vollständigen `scheduled-causal`-Fixpunkte trafen Stored METAR 29.
- Zwei getrennte 29er-Phasen mit zwischenzeitigem Rückgang auf 28 zeigen, dass ein
  erster Rückgang den Tagespeak nicht zuverlässig beendet.
- Late Live lag bei 29,15 °C; ein Netto-Live-Effekt von +0,185 K war bei 1,3 K
  verbleibendem Modellanstieg und tatsächlich späterer Rückkehr auf 29 plausibel.
- TAF TX30 blieb an allen Checkpoints ein warmer Runner-up, erzeugte aber keinen
  falschen Modal-Flip. Positive TAF-Upper-Tail-Wirkung getrennt von einem möglichen
  negativen TAF-Dämpfer bei spezifischem Disagreement untersuchen.
- Der Analog-Challenger war gemischt besser beziehungsweise schlechter. Weiter
  checkpointweise beobachten, keine aggregierte Vorab-Promotion.

## Vergleich 16.–19. September

- 16. September: positive Clear-Sky-/Cloud-/Radiation-Signale kumulierten trotz
  passiertem Modellpeak und nur 0,1 K Restanstieg zu stark.
- 17. September: ein großer positiver Late-Live-Uplift war erforderlich und korrekt;
  ein pauschaler Overlap-Cap hätte den einzigen richtigen Checkpoint gefährdet.
- 18. September: ebenfalls nur 0,1 K Restanstieg, aber kein Clear-Sky-Uplift und ein
  netto negativer Live-Effekt; kein Cap wäre nötig gewesen.
- 19. September: 1,3 K Restanstieg und spätere zweite Peakphase; ein pauschaler Cap
  oder früher Peak-Lock hätte zulässige Erwärmung abgeschnitten.

Der Challenger darf positiven Live-Uplift deshalb nicht pauschal deckeln. Er soll
Overlap, Restanstieg, bisherige Peak-Touches, zusammenhängende Plateau-Dauer und eine
spätere Rückkehr zum bisherigen Maximum getrennt prüfen. Diese zusätzlichen Features
werden erst aus vorhandenen kompakten Exports beziehungsweise einer ausdrücklich
freigegebenen bounded Extraktion untersucht; kein automatischer Neon-Backfill.

## 20. September 2026 – echter OOS-Tag 20/30

- Alle vier vollständigen `scheduled-causal`-Checkpoints trafen Stored METAR 31;
  D0 war mit 31,13 °C numerisch am genauesten.
- Das Tagesmaximum war ein einzelner 31er-Touch. Touch und bestätigtes Plateau bleiben
  deshalb getrennte Verlaufsklassen.
- Late Live stieg von Weighted Raw 30,84 über Bias 31,06 auf Champion 31,35 °C;
  Netto-Live +0,279 K, TAF-Effekt 0,000 K.
- TAF TX32 lag einen Bucket über Actual, ohne einen falschen Modal-Flip auszulösen.
  Dieser Tag bleibt das zwingende Gegenbeispiel gegen eine pauschale positive
  TAF-Verstärkung.
- Analog Memory lag Late Live mit 31,21 °C näher, aber ohne Promotionsreife.

## 21. September 2026 – echter OOS-Tag 21/30

- Alle vier vollständigen `scheduled-causal`-Checkpoints führten fälschlich 32;
  Stored METAR erreichte 33 als einzelnen Touch zwischen mehreren 32er-Meldungen.
- Late Live lag bei 31,97 °C und damit −1,03 K zu kalt. Nach dem 16:00-Checkpoint
  folgten tatsächlich noch +3 K Erwärmung bis 18:00 LT.
- Der negative Temperaturanker von −0,101 K deutete den Rückstand zum Modellpfad als
  Dämpfung, obwohl die Erwärmung nur verspätet war. Hohe Reststrahlung, starke
  Vormittagserwärmung und weiterhin offene Heizperiode als mögliche Kontexttrennung
  untersuchen.
- TAF wurde intraday von TX32 auf TX33 nach oben revidiert. Der Late-Live-Effekt von
  +0,112 K ließ 33 dennoch außerhalb von Top 1. Revision und absoluter TX-Wert müssen
  als getrennte Signale bewertet werden.
- Research-Zielmerkmal ergänzen: tatsächliche Erwärmung nach dem Late-Live-Checkpoint.
  Keine Änderung am produktiven Temperaturanker oder TAF-Layer aus diesem Einzeltag.

## TAF-Revision-/Late-Heating-Challenger

Nur als neuer Research-Kandidat untersuchen:

- Richtung und Größe der letzten intraday TAF-TX-Revision;
- Alter der Revision am Checkpoint;
- tatsächlicher Beobachtungsrückstand zum Modellpfad;
- verbleibende Strahlung und Modellanstieg;
- starke Vormittagserwärmung versus echte Unterdrückung;
- Erwärmung nach Late Live;
- Top-1-/Top-2-/Top-3-Masse einschließlich Zwischenbucket.

Der 20. September ist die Kontrollgruppe: ein wärmeres absolutes TAF ohne bestätigende
Live-Evidenz durfte den Modal-Bucket nicht verschieben. Der 21. September ist der
Kandidatenfall: neue Aufwärtsrevision plus plausible Restheizung. Keine harte Regel,
keine nachträgliche Änderung alter Shadow-Entscheidungen und kein automatischer
historischer Backfill.

## 22. September 2026 – echter OOS-Tag 22/30

- D−1 und D0 führten 33; First Live und Late Live trafen Stored METAR 34. Alle vier
  Checkpoints waren vollständig `scheduled-causal` und nicht rekonstruiert.
- Nach einem kurzen Rückgang auf 32 entstand von 18:00 bis 19:00 LT ein dreifach
  bestätigtes 34er-Plateau. Der Rückgang beendete den Tagespeak erneut nicht.
- Weighted Raw blieb 0,81–1,10 K zu kalt; Bias verbesserte, reichte allein aber nicht.
  D0 nutzte acht von neun Modellen ohne Meteoblue und war `stale`, die Evidenzklasse
  blieb dennoch vollständig.
- First Live traf durch einen Uplift von +1,084 K. Late Live erreichte 33,78 °C mit
  +0,511 K gegenüber Bias; Clear Sky, Strahlung, Wind und Cloud-Surprise überwogen
  negative Heizrate und negativen Temperaturanker.
- Late Live waren `remaining_model_rise_c=0`, `Model Ceiling Reached Early` und eine
  negative kurzfristige Heizrate aktiv. Trotzdem folgte mindestens 1 K zusätzliche
  Erwärmung. Reststrahlung und extrem trockene klare Luft als Kontextmerkmale prüfen.
- TAF TX34 traf Actual, hatte aber 0,0 K direkten Center-Effekt. Live-Evidenz brachte
  First und Late in den richtigen Bucket; daraus keine pauschale TAF-Verstärkung.
- `Phase-vs-Amplitude` war First und Late aktiv. Den Phase-Ramp-Challenger weiter
  OOS validieren, ohne Promotion oder produktiven Einfluss.
- Collector Coverage 30/30 und erfolgreicher Closeout; isolierte frühe TAF-Timeouts.
  AEMET blieb wegen HTTP 503 unbekannt.

Als zusätzliche Research-Verlaufsklasse speichern: spätes bestätigtes Plateau nach
kurzem Rückgang, insbesondere wenn der modellierte Restanstieg 0 K beträgt. Clear-Sky-
Uplift getrennt nach späterem Plateau und Einmal-Touch auswerten. Die Ableitung erfolgt
nur aus vorhandenen kompakten Exportdaten oder einer ausdrücklich freigegebenen,
begrenzten Extraktion.

## 23. September 2026 – echter OOS-Tag 23/30

- D−1, First Live und Late Live trafen Stored METAR 34; D0 führte knapp 33. Alle
  Checkpoints waren vollständig `scheduled-causal` und nicht rekonstruiert.
- First Live lag mit 33,92 °C nahezu exakt; Late Live mit 34,30 °C ebenfalls nahe am
  Actual. Die Live-Stufe korrigierte die durchgehend kalten Raw-/Bias-Center erfolgreich.
- Drei aufeinanderfolgende 34-°C-Meldungen von 17:30 bis 18:30 LT bestätigen ein
  Plateau und keinen isolierten Heat Spike.
- D−1 war mit 34 zu 33 praktisch unentschieden: 43,82 % zu 43,79 %. Der richtige
  Top-1-Treffer wird als `high_uncertainty` und ausdrücklich nicht als High Confidence
  gewertet.
- First Live nutzte eine beobachtete 60-Minuten-Heizrate von 4 K/h für einen Uplift
  von +0,535 K. Heizraten ab 4 K/h gegen anschließende Plateau-Bildung auswerten.
- Late Live war der Uplift von +0,992 K bei 1,2 K erwartetem Restanstieg, offenem
  Heizfenster, Clear Sky und Reststrahlung berechtigt. Clear-Sky-Wirkung gegen
  tatsächliche Peak-Dauer und nicht nur gegen den finalen Bucket validieren.
- TAF TX34 traf Actual bei D−1, D0 und First Live, hatte aber 0,0 K direkten
  Center-Effekt. D0 blieb knapp bei 33; korrektes TAF-TX bestimmt den Modal-Bucket
  damit weiterhin nicht automatisch.
- Das neueste Late-Live-TAF enthielt kein TX. `taf_tx_missing_latest_revision` als
  separates Datenqualitätsmerkmal untersuchen, ohne fehlende Werte zu rekonstruieren.
- `Persistent Hot` war überall aktiv; `Regional Cluster` D−1 und Late Live. Beide
  einzeln und auf mögliche Überlappung OOS kalibrieren.
- Analog Memory war D0 und First Live etwas besser, Late Live schlechter als der
  Champion. Keine Promotion. AEMET blieb wegen HTTP 503 unbekannt.

Checkpointbezogene Anzeigezustände ändern nichts an der Promotionsregel: Regime Memory
bleibt bei 23/30 insgesamt `WATCH/SHADOW` und ist nicht promotionsberechtigt.

## 24. September 2026 – echter OOS-Tag 24/30

- D−1 und D0 führten 34; First Live und Late Live trafen Stored METAR 35. Alle
  Checkpoints waren vollständig `scheduled-causal` und nicht rekonstruiert.
- Late Live lag mit 34,96 °C nahezu exakt. First Live führte 35 mit 45,63 % vor
  34 mit 29,91 %; Late Live 35 mit 46,80 % vor 34 mit 24,93 %.
- Der 35er-Wert war ein einzelner Touch innerhalb eines mehrfach bestätigten
  34er-Plateaus. Als `upper_bucket_single_touch_supported_by_lower_plateau`
  getrennt von einem stabilen Peak-Plateau auswerten.
- Weighted Raw blieb rund 1 K zu kalt und Bias allein im 34er-Bucket. Erst die
  Live-Evidenz brachte First und Late Live in den korrekten Upper Bucket.
- First Live stieg gegenüber Bias um +0,645 K, Late Live um +0,802 K. Clear Sky,
  Strahlung, Wind, positiver Temperaturanker, `Persistent Hot` und
  `Regional Cluster` unterstützten die nahezu exakte Late-Live-Prognose.
- Late Live lag das beobachtete Maximum erst bei 33 °C. Trotz nur 0,9 K
  modelliertem Restanstieg folgten tatsächlich noch +2 K. Restanstieg deshalb nicht
  isoliert als Cap-Signal verwenden; Reststrahlung und warmes klares Regime bleiben
  notwendige Kontextmerkmale.
- `Persistent Hot` war an allen Checkpoints aktiv; `Regional Cluster` D0 und Late
  Live. Clear Sky, positiver Anchor und Cluster auf mögliche Evidenzüberlappung
  untersuchen, ohne den korrekten Uplift nachträglich als Doppelzählung zu werten.
- TAF wechselte von TX34 bei D−1 auf das korrekte TX35 ab D0, hatte aber weiterhin
  0,0 K direkten Center-Effekt. Bucket-Kalibrierung korrekter TAF-TX-Werte ohne
  direkten Center-Eingriff beobachten; keine pauschale TAF-Verstärkung.
- Analog Memory war D−1, D0 und First Live numerisch näher, Late Live aber klar
  schlechter als der Champion. Weiter checkpointweise auswerten, keine Promotion.
- D−1 fehlte ICON Global, D0 Meteoblue; beide Checkpoints waren `stale`, aber
  kausal und vollständig. Provider-Ausfälle blieben isoliert, der Closeout gelang.
  AEMET blieb wegen HTTP 503 unbekannt und wird nicht rekonstruiert.

Regime Memory steht nun bei 24/30 echten sequenziellen OOS-Tagen. Der Status bleibt
unverändert `WATCH/SHADOW`; 24/30 erzeugt weder Promotionsberechtigung noch eine
produktive Parameteränderung.

## 25. September 2026 – echter OOS-Tag 25/30

- Alle vier vollständigen `scheduled-causal`-Checkpoints führten den korrekten
  Stored-METAR-Bucket 35; kein Checkpoint war rekonstruiert. Late Live war mit
  34,86 °C numerisch am genauesten.
- Drei aufeinanderfolgende 35-°C-Meldungen von 17:00 bis 18:00 LT bestätigen ein
  Peak-Plateau. Der Tag bildet damit die Plateau-Kontrollgruppe zum einzelnen
  Upper-Bucket-Touch am 24. September.
- Weighted Raw blieb mit 34,26–34,53 °C zu kalt; Bias hob auf 34,57–34,80 °C an
  und brachte bereits alle Checkpoints in beziehungsweise nahe an den richtigen
  Center-Bucket.
- Die Live-Stufe blieb kontrolliert: D0 −0,016 K, First Live −0,055 K und Late
  Live +0,175 K. Der korrekte Bucket erforderte insbesondere Late Live keinen
  Clear-Sky Override.
- First Live lag 35 mit 41,18 % nur 3,65 Prozentpunkte vor 34 mit 37,53 %.
  Trotz korrektem Top 1 als `high_uncertainty` klassifizieren, nicht als
  High-Confidence-Treffer.
- Late Live war das beobachtete Maximum erst 34 °C. Bei nur 0,4 K modelliertem
  Restanstieg folgte tatsächlich noch +1 K bis zum Plateau. Dies ergänzt die
  Restanstieg-vs.-tatsächliche-Erwärmung-Kohorte ohne daraus einen pauschalen
  Upper-Tail-Aufschlag abzuleiten.
- `Persistent Hot` war das einzige durchgehend aktive Regime und an diesem Tag
  passend. Tage mit bestätigtem Peak-Plateau getrennt von Einzel-Touch-Tagen
  kalibrieren; den Effekt nicht aus diesem Einzeltag verstärken.
- TAF TX35 traf Actual durchgehend, hatte jedoch 0,0 K direkten Center-Effekt und
  erzeugte keinen Modal-Flip. Als bestätigende Evidenz und hinsichtlich der
  Wahrscheinlichkeitskalibrierung untersuchen, nicht als harte Zielvorgabe.
- Analog Memory lag an drei Checkpoints näher, verlor aber erneut Late Live gegen
  den Champion. Weiter checkpointweise auswerten, keine Promotion.
- D−1 fehlten ICON Global und ARPEGE, D0 ICON Global und Meteoblue. Beide waren
  `stale`, aber kausal und vollständig. Provider-Timeouts blieben isoliert; Coverage
  und Closeout waren erfolgreich. AEMET blieb wegen HTTP 503 unbekannt.

Regime Memory steht nun bei 25/30 echten sequenziellen OOS-Tagen. Es bleibt
`WATCH/SHADOW` und nicht promotionsberechtigt; auch die Annäherung an 30/30 ändert
keine produktive Regel automatisch.

## Weiter beobachten

Clear-Sky/Cloud/Radiation-Overlap; TAF als möglicher Upper-Tail-Dämpfer; Peak-Lock
nach passiertem Modellpeak; Plateau-Dauer versus Touch-Anzahl; zweite Peakphase nach
Rückgang; TAF-gestützte Top-2-Kalibrierung; Temperaturanker bei `Model Ceiling Reached
Early`; Airport Anchor Transfer 0–2 Stunden vor Peak; Analog-Challenger;
TAF-Revisionsrichtung und -alter; Erwärmung nach Late Live; Trading-Hedge-Auswahl;
AEMET-503; `Phase-vs-Amplitude`; Restanstieg 0 K versus spätere Erwärmung;
Clear-Sky-Uplift vor Plateau versus Einmal-Touch; `Persistent Hot`-Kalibrierung;
Clear-Sky-/Regional-Cluster-Overlap; fehlendes TX in der neuesten TAF-Revision;
ein Upper-Bucket-Einzeltouch innerhalb eines niedrigeren Plateaus; Restanstieg 0,9 K
versus tatsächliche +2 K nach Late Live; Clear-Sky-Override 0,0 K als Kontrollgruppe;
Persistent-Hot-Plateau versus Persistent-Hot-Einzeltouch; Restanstieg 0,4 K versus
tatsächliche +1 K nach Late Live; OOS aktuell 25/30 und weiter bis mindestens 30 Tage.
Auch bei 30/30 erfolgt keine automatische Promotion.
