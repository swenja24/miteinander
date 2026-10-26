# Sicherheit

Für einen von außen erreichbaren Betrieb:

1. Miteinander ausschließlich hinter einem Reverse Proxy mit HTTPS veröffentlichen.
2. `COOKIE_SECURE=true`, `SECURE_SSL_REDIRECT=true`, eine öffentliche `APP_BASE_URL`, einen zufälligen `DJANGO_SECRET_KEY` und starke Datenbank- sowie Startpasswörter setzen.
3. Zwei-Faktor-Anmeldung für administrative Konten aktivieren.
4. Port 3080 nicht zusätzlich direkt aus dem Internet erreichbar machen.
5. Einladungs- und Kalenderlinks wie Passwörter behandeln.
6. PostgreSQL und das Upload-Volume regelmäßig verschlüsselt sichern und Wiederherstellungen testen.
7. Betriebssystem, Docker, Arcane und Reverse Proxy aktuell halten.
8. Den unveränderlichen `sha-…`-Image-Tag einsetzen und Updates erst nach einem aktuellen, getesteten Backup einspielen.

Nach zehn fehlgeschlagenen Anmeldeversuchen je Benutzername/IP-Kombination beziehungsweise nach 50 verteilten Versuchen für denselben Benutzernamen sperrt die Anwendung weitere Versuche für 15 Minuten. Für 2FA- und sicherheitsrelevante Kontovorgänge gilt ebenfalls eine Versuchssperre. Der in `compose.yaml` konfigurierte Redis-Cache teilt diese Sperren zwischen den Webprozessen; der Reverse Proxy sollte zusätzlich begrenzen. Sitzungen laufen standardmäßig nach acht Stunden Inaktivität beziehungsweise beim Schließen des Browsers ab.

Persönliche Passwörter und Anzeigenamen können nur vom jeweiligen Konto geändert werden. Ein Bereichsadmin kann Rollen, Modulrechte sowie den Zugang zu seinem Bereich verwalten, aber nicht die globale Identität einer Person übernehmen. Eine serverseitige Passwortwiederherstellung ist mit `python manage.py changepassword BENUTZERNAME` möglich.

Uploads werden nach tatsächlichem Dateityp, Größe und Bildabmessungen geprüft. Geschützte Dateien werden nur nach erneuter Bereichs- und Modulprüfung ausgeliefert. Das Speicherlimit pro Bereich wird mit `WORKSPACE_STORAGE_LIMIT_MB` festgelegt. Es ersetzt kein Monitoring des freien Speicherplatzes auf dem Host.

Einladungs- und Kalenderlinks enthalten geheime Einmaltokens beziehungsweise Zugriffstokens. Gunicorn schreibt deshalb standardmäßig keine Access-Logs. Auch im Reverse Proxy sollten Pfade unter `/api/invitations/` und `/api/calendar-feed/` nicht vollständig protokolliert oder gezielt geschwärzt werden.
