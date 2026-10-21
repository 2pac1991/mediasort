# Mediasort

Eigener FileBot-Ersatz für Unraid: erkennt Filme, Serien und Anime über TMDB und sortiert sie im Plex-Schema ein. Ohne Lizenz, ohne externe Abhängigkeiten (nur Python-Standardbibliothek).

## Was es kann

- Erkennt `S01E02`, `1x02`, `Staffel 1 Folge 2`, Doppelfolgen (`S02E03E04`) und Anime mit absoluter Nummer (`[Gruppe] Titel - 1085`), rechnet diese in Staffel/Folge um
- Holt Titel, Jahr, Folgentitel und Poster von TMDB (deutsche Titel, Fallback auf Englisch)
- Plex-Schema mit `{tmdb-12345}` im Ordnernamen, damit Plex garantiert richtig zuordnet
- Hardlink (kein extra Speicher), Verschieben, Kopieren oder nur Umbenennen; Fallback, wenn Hardlink nicht geht
- Untertitel (`.de.srt`, `.forced.ger.srt`, `Subs/`-Ordner) wandern mit
- Web-Oberfläche mit Vorschau: Quelle → Ziel, Sicherheits-Balken, manuelle Korrektur mit Postersuche
- Watch-Ordner: sichere Treffer automatisch, unsichere landen zum Prüfen im Eingang
- Verlauf mit Rückgängig, Plex-Bibliothek aktualisieren, Webhook (z. B. Home Assistant)
- Samples, unfertige Downloads (`.part`) und kleine Dateien werden ignoriert

## Installation auf Unraid

**1. Vorlage holen** (Unraid → Terminal)

```sh
wget -O /boot/config/plugins/dockerMan/templates-user/my-Mediasort.xml \
  https://raw.githubusercontent.com/2pac1991/mediasort/main/unraid/my-Mediasort.xml
```

**2. Container anlegen**

Docker → Container hinzufügen → Vorlage „Mediasort" wählen → Übernehmen.
Wichtig ist der eine gemeinsame Mount `/mnt/user/Plex → /data`: Downloads und Mediathek müssen im selben Mount liegen, sonst gehen keine Hardlinks.

**3. Einrichten** (Web-Oberfläche auf Port 8484)

- Kostenlosen TMDB-API-Key eintragen (themoviedb.org → Einstellungen → API) und „Testen" drücken
- Zielordner an deine Plex-Bibliotheken anpassen
- Optional Plex-Adresse und Token für automatisches Aktualisieren

## Updates

Jede Änderung auf `main` baut automatisch ein neues Image (`ghcr.io/2pac1991/mediasort:latest`). In Unraid erscheint dann beim Container „Update verfügbar".

## Hinweise

- **Alten FileBot-Watch stoppen**, sonst sortieren beide denselben Download-Ordner.
- Hardlinks auf `/mnt/user` gehen nur, wenn Quelle und Ziel auf derselben Platte landen. Sonst greift der Fallback (Standard: kopieren).
- Mit JDownloader wird nicht geseedet. Wenn der Download-Ordner nicht voll bleiben soll, ist „Verschieben" die bessere Aktion.
- Anmeldung: Beim ersten Aufruf legst du ein Admin-Konto an. In den Einstellungen unter „Nutzer" bekommt jeder Freund ein eigenes Konto, wahlweise mit Löschrecht. Ist `UI_PASSWORD` gesetzt, wird es beim Anlegen des ersten Admins als Setup-Code abgefragt.
- Dateien: Im Reiter „Dateien" lassen sich Downloads, Filme, Serien und Anime durchsuchen, umbenennen und löschen. Gelöschtes landet im Papierkorb (`/data/.mediasort-papierkorb`) und wird nach 14 Tagen endgültig entfernt.
- Webhook-Daten: `{"count": 2, "files": ["Dark (2017) - s02e03 - Geister.mkv", ...]}`

## Benachrichtigung in Home Assistant (für alle Nutzer)

Jeder Nutzer kann sich in seinem eigenen Home Assistant benachrichtigen lassen, wenn etwas einsortiert wurde:

1. In Home Assistant die Vorlage importieren: **Einstellungen → Automationen & Szenen → Blueprints → Blueprint importieren** und diese Adresse einfügen:
   `https://github.com/2pac1991/mediasort/blob/main/homeassistant/mediasort-benachrichtigung.yaml`
2. Aus der Vorlage eine Automation erstellen: eine zufällige **Webhook-ID** ausdenken (z. B. `mediasort-x7k2p9q4m1`) und das **Handy** auswählen.
3. Die Webhook-Adresse zusammensetzen: `https://<eigene Home-Assistant-Adresse>/api/webhook/<Webhook-ID>`. Mit Home Assistant Cloud steht die Adresse unter **Einstellungen → Home Assistant Cloud → Webhooks**.
4. In Mediasort beim eigenen Namen auf **🔔** tippen, die Adresse einfügen und den Test senden.

Mediasort braucht dafür Zugang zum Internet bzw. zur Home-Assistant-Adresse des Freundes.
