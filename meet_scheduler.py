"""
Automatisierter Google Meet Outbound Scheduler
Liest Kontakte aus CSV/Excel und plant Google Meet Meetings via Google Calendar API.

Verwendung:
  python meet_scheduler.py --file contacts.csv
  python meet_scheduler.py --file contacts.xlsx --limit 5 --days-ahead 2
  python meet_scheduler.py --results

Voraussetzungen:
  1. Google Cloud Projekt mit Calendar API aktiviert
  2. credentials.json (OAuth2 Desktop Client) im Projektordner
  3. Beim ersten Start: Browser-Login für Google-Autorisierung
"""

import os
import json
import time
import argparse
import logging
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv
import anthropic

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
REQUIRED_COLUMNS = {"name", "email"}
RESULTS_FILE = "results/meet_results.jsonl"
PAUSE_BETWEEN_REQUESTS = 1  # Sekunden zwischen API-Calls
MEETING_DURATION_MINUTES = 30


def get_calendar_service():
    """OAuth2-Authentifizierung und Google Calendar Service aufbauen."""
    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("credentials.json"):
                raise FileNotFoundError(
                    "credentials.json nicht gefunden. "
                    "Bitte Google OAuth2-Zugangsdaten aus der Google Cloud Console herunterladen."
                )
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)

        with open("token.json", "w", encoding="utf-8") as token:
            token.write(creds.to_json())
        log.info("Google-Autorisierung erfolgreich, token.json gespeichert.")

    return build("calendar", "v3", credentials=creds)


def load_contacts(filepath: str) -> pd.DataFrame:
    """Lädt Kontakte aus CSV oder Excel."""
    if filepath.endswith((".xlsx", ".xls")):
        df = pd.read_excel(filepath)
    else:
        df = pd.read_csv(filepath)

    df.columns = [c.strip().lower() for c in df.columns]
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Fehlende Spalten: {missing}. Benötigt: {REQUIRED_COLUMNS}")

    if "company" not in df.columns:
        df["company"] = ""
    if "status" not in df.columns:
        df["status"] = "pending"
    if "notes" not in df.columns:
        df["notes"] = ""

    return df


def generate_meeting_description(contact: dict, company_name: str, agent_name: str) -> str:
    """Claude generiert eine personalisierte Meeting-Beschreibung."""
    try:
        claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        company_hint = f" von {contact['company']}" if contact.get("company") else ""
        prompt = (
            f"Du bist {agent_name} von {company_name}. "
            f"Schreibe eine kurze, freundliche Beschreibung für eine Google Meet Einladung "
            f"an {contact['name']}{company_hint}. "
            f"Das Meeting dauert {MEETING_DURATION_MINUTES} Minuten und dient einem ersten Kennenlerngespräch. "
            f"Schreibe auf Deutsch. Maximal 3 Sätze. Nur die Beschreibung, kein anderer Text."
        )
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as e:
        log.warning("Claude-Fehler bei Beschreibung für %s: %s", contact["name"], e)
        return (
            f"Wir würden uns freuen, Sie in einem kurzen {MEETING_DURATION_MINUTES}-minütigen "
            f"Gespräch kennenzulernen. {agent_name} von {company_name} freut sich auf den Austausch."
        )


def next_business_slot(days_ahead: int) -> tuple[datetime, datetime]:
    """Gibt den Starttermin (10:00 Uhr) eines Werktages zurück."""
    target = datetime.now() + timedelta(days=days_ahead)
    # Wochenende überspringen
    while target.weekday() >= 5:
        target += timedelta(days=1)

    start = target.replace(hour=10, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=MEETING_DURATION_MINUTES)
    return start, end


def schedule_meeting(
    service,
    contact: dict,
    company_name: str,
    agent_name: str,
    days_ahead: int,
) -> dict | None:
    """Erstellt ein Google Meet Event und verschickt eine Kalendereinladung."""
    start, end = next_business_slot(days_ahead)
    description = generate_meeting_description(contact, company_name, agent_name)

    event = {
        "summary": f"Gespräch mit {company_name}",
        "description": description,
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": "Europe/Berlin",
        },
        "attendees": [
            {"email": contact["email"], "displayName": contact["name"]}
        ],
        "conferenceData": {
            "createRequest": {
                "requestId": f"meet-{contact['email']}-{int(time.time())}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "email", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ],
        },
    }

    try:
        created = service.events().insert(
            calendarId="primary",
            body=event,
            conferenceDataVersion=1,
            sendNotifications=True,
        ).execute()

        meet_link = created.get("hangoutLink", "")
        log.info(
            "Meeting geplant für %s: %s | Meet: %s",
            contact["name"],
            start.strftime("%d.%m.%Y %H:%M"),
            meet_link,
        )
        return {
            "event_id": created["id"],
            "meet_link": meet_link,
            "scheduled_at": start.isoformat(),
            "description": description,
        }
    except HttpError as e:
        log.error("Google Calendar Fehler für %s: %s", contact["name"], e)
        return None


def save_result(contact: dict, meeting_info: dict | None, status: str):
    """Speichert Ergebnis in JSONL-Datei."""
    os.makedirs("results", exist_ok=True)
    entry = {
        "contact": {k: str(v) for k, v in contact.items()},
        "status": status,
        "meeting": meeting_info,
        "timestamp": datetime.now().isoformat(),
    }
    with open(RESULTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def show_results():
    """Zeigt alle geplanten Meetings aus der Ergebnis-Datei an."""
    if not os.path.exists(RESULTS_FILE):
        print("Noch keine Ergebnisse vorhanden.")
        return

    print("\n" + "=" * 90)
    print(f"{'Name':<25} {'Status':<12} {'Termin':<22} {'Meet-Link'}")
    print("=" * 90)

    stats: dict[str, int] = {}
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            name = entry["contact"]["name"]
            status = entry.get("status", "unknown")
            meeting = entry.get("meeting") or {}
            scheduled = (
                meeting.get("scheduled_at", "")[:16].replace("T", " ")
                if meeting
                else "-"
            )
            meet_link = meeting.get("meet_link", "-") if meeting else "-"
            print(f"{name:<25} {status:<12} {scheduled:<22} {meet_link}")
            stats[status] = stats.get(status, 0) + 1

    print("=" * 90)
    print("\nZusammenfassung:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print()


def run(args):
    company_name = os.environ["COMPANY_NAME"]
    agent_name = os.environ["AGENT_NAME"]

    service = get_calendar_service()
    df = load_contacts(args.file)

    pending = df[df["status"] == "pending"]
    if args.limit:
        pending = pending.head(args.limit)

    total = len(pending)
    if total == 0:
        log.info("Keine ausstehenden Kontakte gefunden.")
        return

    log.info("Plane %d Google Meet Meetings (Start in %d Tag(en))...", total, args.days_ahead)

    for i, (_, row) in enumerate(pending.iterrows(), 1):
        contact = row.to_dict()
        contact["email"] = str(contact["email"]).strip()
        log.info("[%d/%d] Plane Meeting für: %s (%s)", i, total, contact["name"], contact["email"])

        # Jeder Kontakt bekommt einen eigenen Tag
        days_ahead = args.days_ahead + (i - 1)
        meeting_info = schedule_meeting(service, contact, company_name, agent_name, days_ahead)
        status = "scheduled" if meeting_info else "failed"
        save_result(contact, meeting_info, status)

        if i < total:
            time.sleep(PAUSE_BETWEEN_REQUESTS)

    log.info("Fertig! Ergebnisse anzeigen: python meet_scheduler.py --results")


def main():
    parser = argparse.ArgumentParser(
        description="Automatisierter Google Meet Outbound Scheduler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--file", help="CSV oder Excel-Datei mit Kontakten (Spalten: name, email)")
    parser.add_argument("--limit", type=int, help="Maximale Anzahl zu planender Meetings")
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=1,
        dest="days_ahead",
        help="Erster Termin in X Werktagen ab heute (Standard: 1)",
    )
    parser.add_argument("--results", action="store_true", help="Bisherige Ergebnisse anzeigen")
    args = parser.parse_args()

    if args.results:
        show_results()
    elif args.file:
        run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
