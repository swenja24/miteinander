# Datenschutz und Datenhoheit

Miteinander speichert besonders schutzbedürftige persönliche Daten. Die betreibende Stelle muss vor dem Einsatz Rechtsgrundlage, Empfänger*innen, Aufbewahrungsfristen und Betroffenenrechte dokumentieren.

- Persönliche Bereiche sind voneinander getrennt; Zugriff entsteht nur durch eine Mitgliedschaft.
- Eine pausierte Mitgliedschaft sperrt nur den betroffenen Bereich, nicht andere unabhängige Bereiche derselben Person.
- Gemeinsame WG-Bereiche enthalten ausschließlich dort ausdrücklich angelegte Daten.
- Die Betreiberübersicht zeigt nur Bereichsname, Typ, Status und Anzahl der Zugänge.
- Berechtigte Personen können eine ZIP-Datenkopie ihres aktiven Bereichs mit strukturierten JSON-Daten und allen zugehörigen Dateien herunterladen.
- Eine Löschanforderung sperrt den Bereich; die Betreiberverwaltung kann sie innerhalb von 30 Tagen widerrufen. Danach entfernt der tägliche Hintergrundlauf Daten und zugehörige Uploads.
- Änderungsprotokolle bleiben standardmäßig 730 Tage erhalten. `AUDIT_RETENTION_DAYS` ändert diese Frist.

Backups und hochgeladene Dateien benötigen ein eigenes Löschkonzept. Sicherungen sollten verschlüsselt, zugriffsbeschränkt und zeitlich begrenzt aufbewahrt werden. Für Reverse Proxy, Mailanbieter, Hosting und Backup-Speicher ist zu prüfen, ob Auftragsverarbeitungsverträge erforderlich sind.

Verbesserungswünsche werden installationsweit gespeichert und sind für die Betreiberverwaltung sichtbar. Sie enthalten Anzeigename, Zeitpunkt, Kategorie und Nachricht, aber keine fachlichen Bereichsinhalte. Nutzende werden aufgefordert, dort keine personenbezogenen oder medizinischen Daten einzutragen.
