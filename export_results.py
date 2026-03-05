"""
Exportiert die Anruf-Ergebnisse aus results.jsonl in eine Excel-Datei.

Verwendung:
  python export_results.py
  python export_results.py --output meine_ergebnisse.xlsx
"""

import os
import json
import argparse
from datetime import datetime
import pandas as pd


def main():
    parser = argparse.ArgumentParser(description="Ergebnisse als Excel exportieren")
    parser.add_argument("--output", default=None, help="Ausgabedatei (standard: results_DATUM.xlsx)")
    args = parser.parse_args()

    results_file = "results/results.jsonl"
    if not os.path.exists(results_file):
        print("Keine Ergebnisse gefunden.")
        return

    rows = []
    with open(results_file, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            contact = entry["contact"]
            rows.append({
                "Name": contact.get("name", ""),
                "Telefon": contact.get("phone", ""),
                "E-Mail (alt)": contact.get("email", ""),
                "Firma": contact.get("company", ""),
                "Status": entry.get("status", ""),
                "Person noch da": entry.get("person_still_there"),
                "E-Mail (korrigiert)": entry.get("correct_email") or "",
                "CallSid": entry.get("call_sid", ""),
            })

    df = pd.DataFrame(rows)

    output = args.output or f"results/results_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    df.to_excel(output, index=False)
    print(f"Exportiert: {output} ({len(df)} Einträge)")


if __name__ == "__main__":
    main()
