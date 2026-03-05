"""
CRM-Bereinigung Anrufsystem – Anruf-Starter
Liest Kontakte aus einer CSV/Excel-Datei und startet die Twilio-Anrufe.

Verwendung:
  python caller.py --file contacts.csv
  python caller.py --file contacts.xlsx --limit 10
  python caller.py --results  # Zeigt bisherige Ergebnisse an
"""

import os
import sys
import json
import time
import argparse
import logging
import pandas as pd
from twilio.rest import Client
from dotenv import load_dotenv

# app.py importieren für active_calls (wenn im selben Prozess)
# Beim separaten Betrieb: caller.py startet Anrufe, app.py empfängt Webhooks
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"name", "phone", "email"}
PAUSE_BETWEEN_CALLS = 2  # Sekunden zwischen Anrufen


def load_contacts(filepath: str) -> pd.DataFrame:
    """Lädt Kontakte aus CSV oder Excel."""
    if filepath.endswith(".xlsx") or filepath.endswith(".xls"):
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

    return df


def make_call(client: Client, contact: dict, public_url: str, from_number: str) -> str | None:
    """Startet einen einzelnen Anruf und gibt die CallSid zurück."""
    # Kontakt-Info als URL-Parameter übergeben
    public_url = public_url.rstrip("/")
    try:
        call = client.calls.create(
            to=contact["phone"],
            from_=from_number,
            url=f"{public_url}/call/start",
            status_callback=f"{public_url}/call/status",
            status_callback_method="POST",
            status_callback_event=["completed", "no-answer", "busy", "failed"],
        )
        log.info("Anruf gestartet: %s → CallSid: %s", contact["name"], call.sid)
        return call.sid
    except Exception as e:
        log.error("Fehler beim Anruf für %s: %s", contact["name"], e)
        return None


def register_call(call_sid: str, contact: dict):
    """Registriert den Anruf in der gemeinsamen active_calls-Datenstruktur via Datei."""
    os.makedirs("results", exist_ok=True)
    pending_file = "results/pending_calls.json"

    if os.path.exists(pending_file):
        with open(pending_file, "r", encoding="utf-8") as f:
            pending = json.load(f)
    else:
        pending = {}

    pending[call_sid] = {
        "contact": contact,
        "status": "calling",
        "history": [],
        "correct_email": None,
        "person_still_there": None,
    }

    with open(pending_file, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=2)


def show_results():
    """Zeigt alle Ergebnisse aus results.jsonl an."""
    results_file = "results/results.jsonl"
    if not os.path.exists(results_file):
        print("Noch keine Ergebnisse vorhanden.")
        return

    stats = {"confirmed": 0, "email_corrected": 0, "person_left": 0, "no-answer": 0, "other": 0}
    print("\n" + "=" * 70)
    print(f"{'Name':<25} {'Status':<20} {'E-Mail Korrektur'}")
    print("=" * 70)

    with open(results_file, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            name = entry["contact"]["name"]
            status = entry.get("status", "unknown")
            email_fix = entry.get("correct_email") or "-"

            print(f"{name:<25} {status:<20} {email_fix}")

            if status in stats:
                stats[status] += 1
            else:
                stats["other"] += 1

    print("=" * 70)
    print("\nZusammenfassung:")
    for k, v in stats.items():
        if v:
            print(f"  {k}: {v}")
    print()


def run(args):
    twilio_sid = os.environ["TWILIO_ACCOUNT_SID"]
    twilio_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ["TWILIO_FROM_NUMBER"]
    public_url = os.environ["PUBLIC_URL"]

    client = Client(twilio_sid, twilio_token)
    df = load_contacts(args.file)

    # Nur ausstehende Kontakte
    pending = df[df["status"] == "pending"]
    if args.limit:
        pending = pending.head(args.limit)

    total = len(pending)
    log.info("Starte %d Anrufe...", total)

    for i, (_, row) in enumerate(pending.iterrows(), 1):
        contact = row.to_dict()
        contact["phone"] = str(contact["phone"]).strip()
        log.info("[%d/%d] Rufe an: %s (%s)", i, total, contact["name"], contact["phone"])

        call_sid = make_call(client, contact, public_url, from_number)
        if call_sid:
            register_call(call_sid, contact)

        if i < total:
            time.sleep(PAUSE_BETWEEN_CALLS)

    log.info("Alle Anrufe gestartet. Ergebnisse werden in results/results.jsonl gespeichert.")
    log.info("Zum Anzeigen: python caller.py --results")


def main():
    parser = argparse.ArgumentParser(description="CRM-Bereinigung Anrufsystem")
    parser.add_argument("--file", help="CSV oder Excel-Datei mit Kontakten")
    parser.add_argument("--limit", type=int, help="Maximale Anzahl Anrufe")
    parser.add_argument("--results", action="store_true", help="Ergebnisse anzeigen")
    args = parser.parse_args()

    if args.results:
        show_results()
    elif args.file:
        run(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
