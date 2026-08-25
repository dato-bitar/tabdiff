# MASTER-PROMPT 2 — "tabdiff": Lokal-first Diff für tabellarische Daten

## 0. Betriebsmodus (gilt für den gesamten Lauf)

Du arbeitest **vollständig autonom** über mehrere Stunden. Du stellst **keine Rückfragen**. Jede
Entscheidung, die dieser Prompt nicht vorgibt, triffst du selbst, dokumentierst sie in
`DECISIONS.md` und arbeitest weiter.

Regeln:

1. **Niemals blockieren.** Fehlende Dependency, sperrige API, kaputter Testcontainer: Problem in
   `DECISIONS.md` notieren, Fallback wählen, weitermachen. Ein blockiertes Teilziel stoppt nie
   den Gesamtlauf.
2. **Messen statt raten.** Performance-Behauptungen nur mit Benchmark. Verhalten einer Bibliothek
   nur mit Test.
3. **Nach jedem Meilenstein**: `PROGRESS.md` aktualisieren, commiten, Tests laufen lassen.
4. **Grün bleiben.** `make check` nach jedem Commit. Rot schlägt Feature.
5. **Kein Fake.** Keine immer-grünen Tests, keine erfundenen Erwartungswerte, keine
   `TODO`-Stubs, die als fertig gelten. Unfertiges gehört nach `PROGRESS.md` unter
   "Nicht implementiert".
6. **Zeitbudget**: ca. 10 Stunden. Die Meilensteinverteilung ist verbindlich. Bei Zeitdruck
   Scope von hinten kürzen, nie Qualität von vorne.

---

## 1. Mission

Baue **tabdiff**: ein CLI-Tool und eine Bibliothek, die zwei tabellarische Datenquellen
zeilen- und wertgenau vergleichen — lokal, ohne Cloud, ohne Account, ohne dass die Daten die
Maschine verlassen.

Der Hintergrund: Datafolds `data-diff` hat das Problem gelöst, aber das Open-Source-Repo ist
stehengeblieben; die Weiterentwicklung findet in der kommerziellen Cloud-Variante statt. Wer
heute lokal ein Parquet gegen eine Postgres-Tabelle vergleichen will, hat keine gepflegte
Option. Genau die baust du.

**Das Endprodukt** vergleicht:

- Parquet ↔ Parquet
- CSV ↔ CSV
- Postgres-Tabelle ↔ Postgres-Tabelle (auch über zwei verschiedene Verbindungen)
- beliebige Kreuzkombination der obigen
- DuckDB-Datei ↔ alles davon

und liefert: Schema-Diff, Zeilenzahl-Diff, Wert-Diff auf Zellebene, Statistik-Drift pro Spalte
und einen Exit-Code, der sich für CI eignet.

**Erfolgsdefinition:** `tabdiff` vergleicht zwei Parquet-Dateien mit **10 Millionen Zeilen und
20 Spalten** auf einem normalen Laptop in **unter 60 Sekunden** und findet dabei jede injizierte
Abweichung eines synthetischen Testdatensatzes — vollständig, ohne Falschmeldung.

---

## 2. Nicht-Ziele

Baue **nicht**:

- eine Web-UI oder ein Dashboard,
- eine Cloud-Komponente, Telemetrie, Account-System,
- dbt-Integration (kommt später, nicht jetzt),
- Unterstützung für Snowflake, BigQuery, Redshift (Adapter-Trait vorbereiten, aber nicht bauen),
- eine eigene Query-Engine — DuckDB macht die Arbeit,
- Datenreparatur oder Synchronisation. Nur lesen, nur vergleichen.

---

## 3. Technische Vorgaben (nicht verhandelbar)

- **Sprache**: Python 3.12. Kein Rust, kein Go.
- **Paketname**: `tabdiff`. Layout: `src/tabdiff/`, Tests in `tests/`.
- **Build/Deps**: `uv` mit `pyproject.toml`. Kein Poetry, kein requirements.txt.
- **Rechenkern**: **DuckDB**. Alles Schwere läuft als SQL in DuckDB, nicht in Python-Schleifen.
  Das ist die zentrale Architekturentscheidung — halte dich daran. Parquet und CSV liest DuckDB
  nativ; Postgres über die `postgres_scanner`-Extension.
- **Datenaustausch**: PyArrow für alles, was Python anfassen muss. Niemals pandas im Hot Path.
  pandas höchstens im Report-Rendering, und auch da lieber nicht.
- **Lizenz**: MIT.
- **Typisierung**: vollständige Type Hints, `mypy --strict` muss durchlaufen.
- **Lint/Format**: `ruff` (check + format), Konfiguration in `pyproject.toml`.
- **Tests**: `pytest`, `hypothesis` für Property-Tests, `testcontainers` für Postgres.
- **CLI**: `typer`.
- **Ausgabe**: `rich` für Terminal, plus maschinenlesbares JSON.
- **CI**: GitHub Actions, Jobs: ruff, mypy, pytest (mit Postgres-Service-Container).
- **Makefile**: `check` (ruff+mypy+pytest), `bench`, `demo`.

---

## 4. Die zwei Diff-Strategien

Implementiere beide, mit automatischer Auswahl und manuellem Override per Flag.

### 4.1 `joindiff` — für Quellen in derselben Engine

Wenn beide Seiten in derselben DuckDB-Session erreichbar sind (zwei Parquet-Dateien, zwei
Tabellen derselben Postgres-Instanz, Mischformen über DuckDB-Attach), mach einen **FULL OUTER
JOIN auf den Primärschlüssel** und vergleiche spaltenweise in SQL.

Vorteil: exakt, ein Durchgang, kein Netzwerk-Roundtrip pro Zeile.

### 4.2 `hashdiff` — für getrennte Quellen

Wenn die Daten nicht in eine Engine passen (zwei entfernte Postgres-Instanzen, deren Daten du
nicht kopieren willst), nutze **hierarchisches Checksum-Bisecting**:

1. Teile den Schlüsselraum in Segmente.
2. Berechne pro Segment auf beiden Seiten eine Prüfsumme (in der jeweiligen DB, nicht lokal).
3. Segmente mit gleicher Prüfsumme werden verworfen.
4. Segmente mit unterschiedlicher Prüfsumme werden rekursiv weiter geteilt, bis zur
   Blattgröße (Default 8192 Zeilen).
5. Nur die Blätter mit Unterschieden werden tatsächlich gezogen und zeilenweise verglichen.

Das ist der Algorithmus, der die Sache bei großen, weitgehend identischen Tabellen billig macht.
Bau ihn richtig: die Prüfsummenberechnung muss **auf beiden Seiten dieselbe Semantik** haben,
sonst meldest du überall Unterschiede. Das ist die häufigste Fehlerquelle — schreib dafür einen
expliziten Test, der eine Postgres-Tabelle mit einer identischen Parquet-Datei vergleicht und
null Unterschiede finden muss.

**Auswahlheuristik**: Wenn beide Seiten lokal oder in derselben Engine liegen → joindiff. Sonst
hashdiff. Override per `--strategy {auto,join,hash}`.

---

## 5. Typnormalisierung

Der Teil, an dem Data-Diff-Tools in der Praxis scheitern. Nimm ihn ernst.

- **Numerisch**: `--tolerance-abs` und `--tolerance-rel`, Default beide 0 (exakt). Wenn gesetzt,
  gilt ein Wertepaar als gleich, wenn eine der beiden Toleranzen greift. `DECIMAL` und `FLOAT`
  dürfen nicht stillschweigend zusammenfallen — melde einen Typunterschied im Schema-Diff, auch
  wenn die Werte gleich sind.
- **NULL vs leerer String vs Whitespace**: drei verschiedene Dinge. Niemals gleichsetzen.
  Optionales Flag `--treat-empty-as-null` für Leute, deren CSV-Export das durcheinanderbringt.
- **Zeitstempel**: Zeitzonen sind die Hölle. Regel: Vergleiche immer in UTC. Ein
  `TIMESTAMP WITHOUT TIME ZONE` gegen ein `TIMESTAMPTZ` ist ein Schema-Unterschied — melde ihn
  laut, und vergleiche die Werte erst nach expliziter Annahme (`--assume-tz=Europe/Berlin`), die
  im Report auftaucht.
- **Präzision**: Postgres-`timestamp` hat Mikrosekunden, Parquet je nach Writer Millisekunden
  oder Nanosekunden. Truncation-Verhalten explizit machen, per Flag steuerbar,
  Default: auf die gröbere der beiden Präzisionen runden und das im Report vermerken.
- **Strings**: Unicode-NFC vor dem Vergleich. Collation-Unterschiede ignorieren (wir vergleichen
  Werte, nicht Sortierordnung).
- **Booleans**: `true`/`1`/`'t'`/`'yes'` — der Adapter normalisiert, das Ergebnis ist
  ein echter Boolean.
- **JSON-Spalten**: semantisch vergleichen (Schlüsselreihenfolge egal, Whitespace egal), nicht
  als String. Nutze DuckDBs JSON-Funktionen dafür.

---

## 6. Was der Diff ausgibt

Vier Ebenen, jede einzeln abschaltbar:

1. **Schema-Diff**: Spalten nur links, nur rechts, Typunterschiede, Nullability, Reihenfolge
   (als Hinweis, nicht als Fehler).
2. **Zählungen**: Zeilen links, rechts, nur links, nur rechts, in beiden.
3. **Wert-Diff**: für Zeilen mit gleichem Schlüssel — welche Spalten unterscheiden sich, mit
   Beispielen. Default 20 Beispiele pro Spalte, per Flag erhöhbar. Vollständige Ausgabe nur mit
   `--full`, weil das bei großen Diffs den Terminal sprengt.
4. **Spaltenstatistik-Drift**: pro Spalte min, max, Mittelwert, Anzahl NULLs, Anzahl distinkter
   Werte, links vs rechts. Das fängt Fälle, in denen die Schlüssel identisch sind, sich aber die
   Verteilung verschoben hat.

**Exit-Codes**: `0` = identisch, `1` = Unterschiede gefunden, `2` = Fehler (Schema inkompatibel,
Verbindung fehlgeschlagen, Schlüssel nicht eindeutig). Das macht es CI-tauglich.

**Ausgabeformate**: `--format {rich,json,markdown}`. JSON ist stabil und versioniert (Feld
`schema_version`), damit Skripte darauf aufbauen können.

---

## 7. Schlüsselbehandlung

- `--key col1,col2` für zusammengesetzte Schlüssel.
- Wenn kein Schlüssel angegeben ist: versuche einen zu erraten (Primärschlüssel aus dem
  Postgres-Katalog, sonst eine Spalte namens `id`/`uuid`/`pk`, sonst Fehler mit hilfreicher
  Meldung). Rate nie stillschweigend — sag im Output, welchen Schlüssel du benutzt.
- **Prüfe Eindeutigkeit.** Wenn der Schlüssel nicht eindeutig ist, brich mit Exit-Code 2 ab und
  zeig drei Beispiel-Duplikate. Ein Diff auf nicht-eindeutigem Schlüssel produziert Unsinn und
  ist schlimmer als kein Diff.
- `--key-less`-Modus: kein Schlüssel vorhanden. Dann vergleiche als Multimengen über
  Zeilen-Hashes — sag aber klar, dass du dann nur "diese Zeile fehlt / ist neu" sagen kannst,
  nicht "diese Zelle hat sich geändert".

---

## 8. Testdaten und Verifikation

**Baue einen Generator** (`tests/gen.py`), der synthetische Datensätze mit **injizierten,
bekannten Abweichungen** erzeugt. Das ist dein Wahrheitsmaßstab — ohne ihn kannst du nicht
behaupten, dass das Tool funktioniert.

Injektionstypen, jeder einzeln testbar:
`row_added`, `row_deleted`, `value_changed`, `null_introduced`, `type_widened`,
`column_added`, `column_dropped`, `column_renamed`, `precision_lost`, `timezone_shifted`,
`encoding_mangled`, `duplicate_key_introduced`, `order_shuffled` (darf **kein** Diff sein).

Für jeden Typ ein Test: Generator injiziert N Abweichungen → tabdiff findet exakt diese N,
keine mehr, keine weniger.

Zusätzlich:
- **Property-Test mit hypothesis**: eine Tabelle gegen sich selbst diffen ergibt immer null
  Unterschiede, egal wie die Daten aussehen. Das ist der wichtigste Test im ganzen Projekt.
- **Roundtrip-Test**: Postgres-Tabelle → Parquet exportieren → beide diffen → null Unterschiede.
- **Skalentest**: 10 Mio Zeilen, gemessen, Ergebnis in `BENCHMARKS.md`.

---

## 9. Meilensteine (verbindliche Zeitverteilung)

| # | Ziel | Budget | Abnahmekriterium |
|---|------|--------|------------------|
| M0 | Projektgerüst, uv, ruff, mypy, CI, Makefile, Lizenz | 0:30 | `make check` grün |
| M1 | Source-Abstraktion + Parquet/CSV/DuckDB-Adapter | 1:00 | Jede Quelle liefert Arrow-Schema + Zeilenzahl |
| M2 | Schema-Diff + Typnormalisierung + Tests | 1:15 | ≥30 Tests zur Normalisierung, grün |
| M3 | Testdatengenerator mit allen 13 Injektionstypen | 1:00 | Generator erzeugt reproduzierbar per Seed |
| M4 | joindiff über DuckDB | 1:30 | Alle 13 Injektionstypen korrekt erkannt |
| M5 | Postgres-Adapter + testcontainers-Setup | 1:00 | Roundtrip-Test Postgres↔Parquet grün |
| M6 | hashdiff mit Checksum-Bisecting | 1:30 | Identische Tabellen → 0 Diffs; injizierte → alle gefunden |
| M7 | Spaltenstatistik-Drift | 0:30 | Statistik für alle Typen, Snapshot-Test |
| M8 | Ausgabeformate rich/json/markdown, Exit-Codes | 0:45 | JSON-Schema stabil, Snapshot-Test |
| M9 | Skalen-Benchmark, README, Aufräumen | 1:00 | 10M Zeilen <60s dokumentiert |

Bei Zeitdruck: M7 darf entfallen. **M6 darf nicht entfallen** — ohne hashdiff ist es nur ein
DuckDB-Wrapper. Wenn Postgres via testcontainers zickt, weiche auf eine lokal installierte
Instanz oder auf zwei getrennte DuckDB-Dateien als Ersatz für "zwei getrennte Engines" aus und
dokumentiere das.

---

## 10. Definition of Done

- [ ] `make check` grün: ruff, `mypy --strict`, pytest
- [ ] ≥120 Tests, davon ≥3 Property-Tests
- [ ] Alle 13 Injektionstypen haben je einen bestehenden Test
- [ ] Selbst-Diff ergibt immer null Unterschiede (Property-Test)
- [ ] Beide Strategien implementiert und getestet
- [ ] 10M-Zeilen-Benchmark gemessen, Zahl steht in `BENCHMARKS.md` mit Hardware-Angabe
- [ ] Exit-Codes 0/1/2 korrekt, mit Test
- [ ] `README.md`: Problem, Lösung, Installation, fünf Beispielaufrufe mit echter Ausgabe,
      Abschnitt "Wie sich das von data-diff unterscheidet", Abschnitt "Bekannte Grenzen"
- [ ] `DECISIONS.md`, `PROGRESS.md` (mit "Nicht implementiert"), `CONTRIBUTING.md`
- [ ] Keine Datenexfiltration: ein Test, der prüft, dass ohne explizite Verbindungsangabe keine
      Netzwerkverbindung aufgebaut wird

## 11. Zum Schluss

Schreibe in `LIMITS.md` ehrlich auf: Bei welchen Datenmengen, Schemaformen oder Typkombinationen
liefert dein Tool falsche oder unbrauchbare Ergebnisse? Wo hast du bewusst vereinfacht? Wenn
jemand das produktiv einsetzen will — was muss er vorher wissen? Sei dabei streng mit dir selbst;
diese Datei ist wertvoller als ein weiteres Feature.
