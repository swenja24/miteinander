# Miteinander

Ein selbst gehosteter MVP zur gemeinsamen Organisation von Eingliederungshilfe in Familien.

[![MIT License](https://img.shields.io/badge/Lizenz-MIT-green.svg)](LICENSE)

## Enthalten

- Anträge und Behördenkontakte mit Status und Fristen
- Aufgaben, Zuständigkeiten und Fälligkeiten
- Dokumentenregister mit Zuordnung zu Anträgen
- Digitales Kassenbuch für mehrere Kassen und Konten, Belegfotos, CSV-Export für Excel und druckbare PDF-Ansicht
- Familienbereich mit beteiligten Personen und Rollen
- Antragsakten mit Ergänzungsanträgen, neutraler Ereignis- und Fristentimeline, Korrespondenz sowie geschützten Bild- und PDF-Anhängen
- Gemeinsamer Bereich „Über Linea“ mit Personenprofil, Zielen, Kommentarverlauf und administrativ freigegebenen Regeln
- Individuelle Zugänge, rollenbasierte Bereichsrechte und persistente Datenspeicherung
- Responsive, tastaturbedienbare Oberfläche

> Hinweis: Der MVP ist ein Organisationswerkzeug und keine Rechts- oder Fachsoftware. Dateien liegen geschützt hinter der Anmeldung, werden im Docker-Volume aber nicht zusätzlich anwendungsseitig verschlüsselt.

## Schnellstart mit Docker Compose

1. `compose.yaml` und `.env.example` auf den Docker-Host kopieren.
2. `.env.example` nach `.env` kopieren und `GHCR_IMAGE` sowie `APP_PASSWORD` ändern.
3. Starten:

   ```bash
   cp .env.example .env
   docker compose pull
   docker compose up -d
   ```

4. Im Browser `http://SERVER-IP:3080` öffnen.

Die Daten liegen im Docker-Volume `miteinander_data`. Der Container kann neu gebaut oder aktualisiert werden, ohne die Daten zu verlieren.

## Automatische Images auf GHCR

Der Workflow `.github/workflows/container.yml` läuft bei jedem Push auf `main`. Er führt die Tests aus, baut das Docker-Image und veröffentlicht zwei Tags:

- `latest` für den jeweils neuesten erfolgreichen Build von `main`
- `sha-…` für einen unveränderlichen, eindeutig einem Commit zugeordneten Build

Das Image wird unter `ghcr.io/GITHUB-NAME/REPOSITORY` veröffentlicht. Nach dem ersten erfolgreichen Lauf in GitHub unter **Packages → Package settings → Change visibility** einmalig die Sichtbarkeit auf **Public** setzen. Danach kann Docker das Image ohne Registry-Zugangsdaten beziehen.

Für ein Rollback in `compose.yaml` statt `:latest` den vorherigen Commit-Tag eintragen, beispielsweise `:sha-a1b2c3d`, und den Stack erneut bereitstellen.

## Deployment mit Arcane

In Arcane einen neuen Stack mit `compose.yaml` anlegen und die Variablen aus `.env.example` als Stack-Umgebungsvariablen konfigurieren. `GHCR_IMAGE` muss vollständig und kleingeschrieben sein, beispielsweise `ghcr.io/mein-name/miteinander`.

Für ein Update in Arcane das neue `latest`-Image ziehen und den Stack neu bereitstellen. `pull_policy: always` sorgt dafür, dass beim Deployment nicht versehentlich der lokale Cache verwendet wird. Das Daten-Volume bleibt dabei erhalten.

Für einen kontrollierteren Produktivbetrieb kann Arcane statt `latest` auch einen konkreten `sha-…`-Tag verwenden. Der Build erfolgt weiterhin bei jedem Push, aber der produktive Wechsel wird dann bewusst vorgenommen.

Für externen Zugriff sollte die App hinter einem Reverse Proxy mit HTTPS laufen. Port `3080` muss dann nicht öffentlich freigegeben werden. Die App setzt bewusst keine `Secure`-Cookie-Markierung, damit sie im lokalen Netz zunächst auch per HTTP funktioniert; bei öffentlichem Betrieb sollte ausschließlich HTTPS verwendet werden.

## Backup und Wiederherstellung

Alle Nutzdaten stehen in `/app/data/familie.json`; Belegbilder liegen unter `/app/data/receipts`, Dateien aus Antragsakten unter `/app/data/case-files` und hochgeladene Dokumente unter `/app/data/document-files`. Ein Backup muss deshalb immer das gesamte Volume enthalten:

```bash
docker run --rm -v miteinander_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/miteinander-backup.tgz -C /data .
```

Wiederherstellung bei gestopptem App-Container:

```bash
docker run --rm -v miteinander_data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/miteinander-backup.tgz -C /data'
```

## Lokale Entwicklung mit Python

Es werden keine externen Python-Pakete benötigt.

```bash
APP_PASSWORD=entwicklung python3 server.py
```

Danach ist die App unter `http://localhost:3000` erreichbar.

Ein lokales Container-Image kann weiterhin unabhängig von GHCR gebaut werden:

```bash
docker build -t miteinander:dev .
```

## Sicherheit vor produktivem Einsatz

- Ein starkes Startpasswort über `APP_PASSWORD` setzen.
- Der erste Zugang heißt `linea` und verwendet beim ersten Start `APP_PASSWORD`. Weitere Zugänge und Bereichsrechte werden in der Familienansicht angelegt.
- Für jede Person einen eigenen Zugang verwenden; Passwörter nicht gemeinsam nutzen.
- HTTPS über einen Reverse Proxy aktivieren.
- Docker-Volume regelmäßig verschlüsselt sichern.
- Zugriff auf den Familienkreis und bei Bedarf VPN/LAN beschränken.
- Vor der Nutzung mit echten Gesundheits- oder Sozialdaten Datenschutz, Löschkonzept, Aufbewahrungsfristen und Berechtigungskonzept fachlich prüfen lassen.

## Lizenz

Miteinander ist Open Source und steht unter der [MIT-Lizenz](LICENSE).

Die mitgelieferte Schriftfamilie Carlito steht separat unter der
[SIL Open Font License 1.1](public/fonts/OFL.txt).
