# Sicherheit

Für einen von außen erreichbaren Betrieb:

1. Miteinander ausschließlich hinter einem Reverse Proxy mit HTTPS veröffentlichen.
2. `COOKIE_SECURE=true`, `SECURE_SSL_REDIRECT=true`, eine öffentliche `APP_BASE_URL`, einen zufälligen `DJANGO_SECRET_KEY` und starke Datenbank- sowie Startpasswörter setzen.
3. Zwei-Faktor-Anmeldung für administrative Konten aktivieren.
4. Port 3080 nicht zusätzlich direkt aus dem Internet erreichbar machen.
5. Einladungs- und Kalenderlinks wie Passwörter behandeln.
6. PostgreSQL und das Upload-Volume regelmäßig verschlüsselt sichern und Wiederherstellungen testen.
7. Betriebssystem, Docker, Arcane und Reverse Proxy aktuell halten.

Nach zehn fehlgeschlagenen Anmeldeversuchen pro Benutzername und IP-Adresse sperrt die Anwendung weitere Versuche für 15 Minuten. Bei mehreren Webprozessen sollte für den Anmelde-Limiter ein gemeinsamer Django-Cache konfiguriert werden; der Reverse Proxy sollte zusätzlich begrenzen.
