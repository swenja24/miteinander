# Sicherheit

Für einen von außen erreichbaren Betrieb:

1. Miteinander ausschließlich hinter einem Reverse Proxy mit HTTPS veröffentlichen.
2. `COOKIE_SECURE=true`, eine öffentliche `APP_BASE_URL` und ein starkes `APP_PASSWORD` setzen.
3. Zwei-Faktor-Anmeldung für administrative Konten aktivieren.
4. Port 3080 nicht zusätzlich direkt aus dem Internet erreichbar machen.
5. Einladungs- und Kalenderlinks wie Passwörter behandeln.
6. Das gesamte Docker-Volume regelmäßig verschlüsselt sichern und Wiederherstellungen testen.
7. Betriebssystem, Docker, Arcane und Reverse Proxy aktuell halten.

Nach acht fehlgeschlagenen Anmeldeversuchen pro Benutzername und IP-Adresse sperrt die Anwendung weitere Versuche für 15 Minuten.
