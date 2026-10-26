# Miteinander

Miteinander ist eine selbst gehostete Webanwendung zur gemeinsamen Organisation von Eingliederungshilfe und ambulant betreutem Wohnen. Persönliche Bereiche, gemeinsame WG-Bereiche, fein abgestufte Rechte und die Perspektive der leistungsberechtigten Person stehen im Mittelpunkt.

## Technische Architektur

- Django 5.2 LTS als modularer Python-Anwendungskern
- Gunicorn als Produktionsserver
- PostgreSQL für transaktionssichere, mandantengetrennte Daten
- Redis und Celery für E-Mail-Hinweise und automatische Löschfristen
- WhiteNoise für statische Dateien
- lokale, geschützte Uploads oder optional S3-kompatibler Objektspeicher
- Docker Compose für Proxmox, Arcane und vergleichbare Umgebungen

Fachlich unterschiedliche Detailfelder bleiben flexibel als JSON in ihrem jeweiligen Datensatz. Ein Antrag, eine Aufgabe oder eine Buchung ist trotzdem immer eine eigene Datenbankzeile mit Bereichszuordnung, Datum, Status und weiteren indexierten Kernfeldern. Es gibt keine gemeinsame JSON-Datei mehr, die bei gleichzeitigen Änderungen überschrieben werden kann.

## Funktionen

- Anträge mit Vorgängen, Korrespondenz, Anhängen, Fristen und Ergänzungsanträgen
- einmalige und wiederkehrende Aufgaben sowie versionierte Tages- und Wochenabläufe
- Kalender mit berechtigungsabhängigen Quellen und persönlichem iCal-Abonnement
- Dokumentenablage mit Kamera-/Scan-Upload
- Kassenbuch für mehrere Konten, Belegfotos, CSV-/CAMT-Import und Export
- persönliche und gemeinsame WG-Bereiche unter einem Zugang
- Profile, Ziele, Regeln, BEIs, Kontakte, Infobrett und thematische Beiträge
- Einladungslinks, je Bereich pausierbare Zugänge, Modulrechte, Zwei-Faktor-Anmeldung und Änderungsprotokoll
- vollständiger Bereichsexport als ZIP mit JSON-Daten und Dateien

Statische Dateien erhalten beim Containerbau automatisch inhaltsabhängige Dateinamen. Dadurch laden Browser nach einem Update zuverlässig die neue Oberfläche; der Service Worker nutzt online immer die aktuelle Fassung und hält nur einen Offline-Rückfall vor.

## Deployment mit Arcane

1. Bei einer bestehenden Installation den vorhandenen Arcane-Stack aktualisieren
   und dessen Volume `miteinander_data` unbedingt beibehalten. Nur bei einer
   komplett neuen Installation einen neuen Stack anlegen. `compose.yaml` und die
   Werte aus `.env.example` in Arcane hinterlegen.
2. Mindestens diese Werte individuell setzen:

   - `POSTGRES_PASSWORD`
   - `DJANGO_SECRET_KEY`
   - `BOOTSTRAP_ADMIN_PASSWORD`
   - `ALLOWED_HOSTS`
   - `CSRF_TRUSTED_ORIGINS`
   - `APP_BASE_URL`

   Zufallswerte lassen sich beispielsweise so erzeugen:

   ```bash
   openssl rand -hex 32
   openssl rand -base64 48
   ```

3. Den Stack hinter einem Reverse Proxy mit HTTPS veröffentlichen. Der Standardport am Host ist `3080` und lässt sich mit `HOST_PORT` ändern.
4. Stack bereitstellen. Die Anwendung führt Datenbankmigrationen selbst aus und legt bei einer leeren Installation den ersten Zugang an.

Die Dienste sind:

- `miteinander`: Django/Gunicorn-Webanwendung
- `database`: PostgreSQL
- `redis`: Aufgabenwarteschlange
- `worker`: E-Mail- und Hintergrundaufgaben
- `scheduler`: tägliche Lösch- und Aufbewahrungsprüfung

## Update und Rollback

Bei jedem Push auf `main` prüft GitHub Actions den Code, baut ein Image und veröffentlicht `latest` sowie einen unveränderlichen `sha-…`-Tag in GHCR. Für einen kontrollierten Produktivbetrieb empfiehlt sich in Arcane ein konkreter `IMAGE_TAG=sha-…`. Vor einem Update immer ein Backup erstellen.

Die Prüfung umfasst Django-Tests, Syntax- und Migrationschecks, einen Abgleich bekannter Python-Sicherheitslücken sowie eine statische Sicherheitsanalyse. Dependabot kontrolliert Python-, Docker- und GitHub-Actions-Abhängigkeiten wöchentlich.

Für ein Update Image und Stack neu bereitstellen. Django-Migrationen laufen vor dem Start von Gunicorn. Für ein Rollback den vorherigen `sha-…`-Tag verwenden. Ein Datenbankschema lässt sich nicht in jedem Fall durch einen reinen Image-Rollback zurückdrehen; deshalb gehört das Backup zwingend zum Updateablauf.

## Übernahme der bisherigen JSON-Installation

Das bisherige Docker-Volume `miteinander_data` bleibt am Pfad `/app/data` eingebunden. Liegt dort `familie.json` und ist PostgreSQL noch leer, importiert die neue Version automatisch:

- Bereiche und Mitgliedschaften
- Zugänge einschließlich einmaliger Passwortmigration beim nächsten Login
- alle Facheinträge und Optionen
- hochgeladene Dokumente, Bilder und Belege
- Einladungen, Kalenderlinks, Feedback und Änderungsprotokolle

Die alte JSON-Datei wird nur gelesen und bleibt als zusätzliche Rückfallkopie erhalten. Der Import ist idempotent und wird übersprungen, sobald ein Bereich in PostgreSQL existiert.

In Arcane muss deshalb der bestehende Stack aktualisiert werden. Wird stattdessen
ein Stack mit anderem Namen angelegt, erzeugt Docker normalerweise auch ein neues,
leeres Volume. Vor dem Start in Arcane kontrollieren, dass der Dienst
`miteinander` wirklich das bisherige Datenvolume nach `/app/data` einbindet.
Zusätzlich das alte Volume über Arcane oder per Proxmox-Backup sichern; erst
danach das neue Image starten.

Vor diesem Versionswechsel ein Backup des bisherigen Volumes erstellen. Nach dem Start mit einem Admin- und einem Assistenzkonto alle sichtbaren Bereiche, einige Anhänge und Kontostände stichprobenartig kontrollieren.

## Backup und Wiederherstellung

Ein vollständiges Backup braucht zwei Bestandteile:

- PostgreSQL-Dump
- Upload-Volume `/app/data`

Im Verzeichnis mit `compose.yaml`:

```bash
./scripts/backup.sh ./backups
./scripts/restore-test.sh ./backups/miteinander-JJJJMMTT-HHMMSS.tgz
```

Das Backupskript erzeugt zusätzlich eine SHA-256-Prüfsumme. `restore-test.sh` stellt den Datenbankdump in einem kurzlebigen PostgreSQL-Container wieder her und prüft das Dateiarchiv, ohne den laufenden Stack zu verändern. Backups müssen verschlüsselt, zugriffsbeschränkt und mit einer dokumentierten Aufbewahrungsfrist gespeichert werden.

Das Skript legt lokale Backupdateien nur für den ausführenden Systembenutzer lesbar an, verschlüsselt sie aber nicht selbst. Speichere sie daher ausschließlich auf einem verschlüsselten Datenträger oder verschlüssele sie unmittelbar mit dem Backup-System deines Proxmox-Servers.

Bei Verwendung eines S3-kompatiblen Objektspeichers muss dessen Inhalt zusätzlich mit den Werkzeugen des Speicheranbieters gesichert und wiederherstellbar getestet werden.

Für einen S3-kompatiblen Speicher werden in `.env` `AWS_STORAGE_BUCKET_NAME`,
`AWS_ACCESS_KEY_ID` und `AWS_SECRET_ACCESS_KEY` gesetzt. Bei Diensten wie MinIO
oder Hetzner Object Storage kommen `AWS_S3_ENDPOINT_URL` und gegebenenfalls
`AWS_S3_REGION_NAME` hinzu. Ohne Bucket-Namen bleiben Uploads im geschützten
Docker-Volume.

## Lokale Entwicklung

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
SECURE_SSL_REDIRECT=false COOKIE_SECURE=false .venv/bin/python manage.py migrate
BOOTSTRAP_ADMIN_PASSWORD='ein-langes-Testpasswort' \
  SECURE_SSL_REDIRECT=false COOKIE_SECURE=false \
  .venv/bin/python manage.py bootstrap_miteinander
SECURE_SSL_REDIRECT=false COOKIE_SECURE=false .venv/bin/python manage.py runserver
```

Tests:

```bash
SECURE_SSL_REDIRECT=false COOKIE_SECURE=false .venv/bin/python manage.py test core.tests
```

Hat eine Person ihr Passwort vergessen, kann ausschließlich die betreibende Stelle mit Serverzugriff ein neues setzen:

```bash
docker compose exec miteinander python manage.py changepassword BENUTZERNAME
```

Bereichsadministrator*innen dürfen aus Gründen der Mandantentrennung keine globalen Passwörter anderer Personen ändern.

## Sicherheit und Datenschutz

Miteinander verarbeitet potenziell Gesundheits-, Sozial- und Finanzdaten. Vor einem echten Einsatz sind unter anderem Rechtsgrundlage, Rollen- und Berechtigungskonzept, Auftragsverarbeitung, Löschfristen, Backup-Verschlüsselung, Wiederherstellung, Vorfallprozess und Datenschutzhinweise fachlich zu prüfen. Technische Hinweise stehen in [SECURITY.md](SECURITY.md), datenschutzbezogene Hinweise in [PRIVACY.md](PRIVACY.md).

Die Schriftfamilie Carlito steht separat unter der [SIL Open Font License 1.1](public/fonts/OFL.txt).

Der derzeit veröffentlichte Quellcode steht gemäß der Datei [LICENSE](LICENSE) unter der MIT-Lizenz. Eine spätere Änderung des Geschäfts- oder Lizenzmodells sollte vor der nächsten Veröffentlichung bewusst entschieden werden; bereits unter MIT veröffentlichte Fassungen lassen sich nicht rückwirkend zurücknehmen.
