# Datenschutz und Datenhoheit

Miteinander speichert besonders schutzbedürftige persönliche Daten. Die betreibende Stelle muss vor dem Einsatz Rechtsgrundlage, Empfänger*innen, Aufbewahrungsfristen und Betroffenenrechte dokumentieren.

- Persönliche Bereiche sind voneinander getrennt; Zugriff entsteht nur durch eine Mitgliedschaft.
- Gemeinsame WG-Bereiche enthalten ausschließlich dort ausdrücklich angelegte Daten.
- Die Betreiberübersicht zeigt nur Bereichsname, Typ, Status und Anzahl der Zugänge.
- Administrator*innen können eine JSON-Datenkopie ihres aktiven Bereichs herunterladen.
- Eine Löschanforderung sperrt den Bereich; die Betreiberverwaltung kann sie innerhalb von 30 Tagen widerrufen. Danach werden Daten und zugehörige Uploads beim nächsten Start entfernt.
- Änderungsprotokolle bleiben standardmäßig 730 Tage erhalten. `AUDIT_RETENTION_DAYS` ändert diese Frist.

Backups und hochgeladene Dateien benötigen ein eigenes Löschkonzept. Sicherungen sollten verschlüsselt, zugriffsbeschränkt und zeitlich begrenzt aufbewahrt werden. Für Reverse Proxy, Mailanbieter, Hosting und Backup-Speicher ist zu prüfen, ob Auftragsverarbeitungsverträge erforderlich sind.
