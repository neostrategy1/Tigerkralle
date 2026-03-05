"""
CRM-Bereinigung Anrufsystem – Twilio Webhook Server
Empfängt Twilio-Webhooks und führt das Gespräch via Claude KI.
"""

import os
import json
import logging
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.request_validator import RequestValidator
import anthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

COMPANY_NAME = os.environ["COMPANY_NAME"]
AGENT_NAME = os.environ["AGENT_NAME"]
TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

PENDING_FILE = "results/pending_calls.json"


def load_active_calls() -> dict:
    """Lädt aktive Anrufe aus Datei (geteilt mit caller.py)."""
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_active_calls(calls: dict):
    """Speichert aktive Anrufe in Datei."""
    os.makedirs("results", exist_ok=True)
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(calls, f, ensure_ascii=False, indent=2)


# Laufende Anrufe: { call_sid: { contact_info, history, state } }
# Wird aus Datei geladen damit caller.py und app.py denselben Stand haben
active_calls: dict = load_active_calls()

SYSTEM_PROMPT = f"""Du bist {AGENT_NAME}, ein freundlicher Telefonassistent von {COMPANY_NAME}.
Deine Aufgabe: CRM-Daten bereinigen, indem du Ansprechpartner und E-Mail-Adressen bestätigst.

Analysiere die Antwort der angerufenen Person und gib ein JSON zurück mit:
{{
  "next_action": "confirm" | "ask_email" | "note_left" | "retry" | "end",
  "correct_email": "<E-Mail falls korrigiert, sonst null>",
  "person_still_there": true | false | null,
  "reply_text": "<Was du als nächstes sagen sollst, auf Deutsch, kurz und freundlich>"
}}

Aktionen:
- "confirm": Person bestätigt – Person ist noch da UND E-Mail stimmt → bedanke dich und verabschiede dich
- "ask_email": Person sagt E-Mail stimmt nicht, du fragst nach der korrekten E-Mail
- "note_left": Person ist nicht mehr im Unternehmen → notiere und verabschiede dich
- "retry": Unklar, frage nochmal nach
- "end": Gespräch beenden (Fehler, kein Interesse etc.)

Antworte NUR mit dem JSON-Objekt, kein anderer Text."""


def twiml_response(text: str, gather_action: str | None = None, end: bool = False) -> str:
    """Erzeugt TwiML mit deutschem Text-to-Speech."""
    vr = VoiceResponse()
    if end:
        vr.say(text, language="de-DE", voice="Polly.Vicki")
        vr.hangup()
    elif gather_action:
        gather = Gather(
            input="speech",
            language="de-DE",
            action=gather_action,
            method="POST",
            timeout=6,
            speech_timeout="auto",
        )
        gather.say(text, language="de-DE", voice="Polly.Vicki")
        vr.append(gather)
        # Fallback falls keine Eingabe
        vr.say("Ich habe Sie leider nicht verstanden. Auf Wiederhören.", language="de-DE", voice="Polly.Vicki")
        vr.hangup()
    else:
        vr.say(text, language="de-DE", voice="Polly.Vicki")
        vr.hangup()
    return str(vr)


def ask_claude(contact: dict, history: list, speech_input: str) -> dict:
    """Claude analysiert die Antwort und entscheidet wie es weitergeht."""
    history.append({"role": "user", "content": f"Antwort der angerufenen Person: \"{speech_input}\""})

    context = f"Kontakt: {contact['name']} | E-Mail: {contact['email']} | Firma: {contact['company']}"

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT + f"\n\nKontext: {context}",
        messages=history,
    )
    raw = response.content[0].text.strip()
    log.info("Claude Antwort: %s", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {
            "next_action": "end",
            "correct_email": None,
            "person_still_there": None,
            "reply_text": "Entschuldigung, es gab einen technischen Fehler. Auf Wiederhören.",
        }
    history.append({"role": "assistant", "content": raw})
    return result


def save_result(call_sid: str, state: dict):
    """Ergebnis in results.jsonl speichern und aus active_calls entfernen."""
    os.makedirs("results", exist_ok=True)
    entry = {
        "call_sid": call_sid,
        "contact": state["contact"],
        "status": state["status"],
        "correct_email": state.get("correct_email"),
        "person_still_there": state.get("person_still_there"),
    }
    with open("results/results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info("Ergebnis gespeichert: %s", entry)
    # Aus aktiver Liste entfernen
    active_calls.pop(call_sid, None)
    save_active_calls(active_calls)


@app.route("/call/start", methods=["POST"])
def call_start():
    """Twilio ruft diesen Endpoint auf wenn die Person abnimmt."""
    call_sid = request.form.get("CallSid", "")
    log.info("Anruf gestartet: %s", call_sid)

    # Neu laden falls caller.py in der Zwischenzeit Anrufe hinzugefügt hat
    active_calls.update(load_active_calls())

    if call_sid not in active_calls:
        log.warning("Unbekannte CallSid: %s", call_sid)
        vr = VoiceResponse()
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    state = active_calls[call_sid]
    contact = state["contact"]
    state["status"] = "in_progress"

    greeting = (
        f"Guten Tag! Mein Name ist {AGENT_NAME} von {COMPANY_NAME}. "
        f"Wir räumen gerade unser CRM-System auf und hätten kurz eine Frage. "
        f"Ist {contact['name']} noch bei Ihnen im Unternehmen, "
        f"und ist die E-Mail-Adresse {contact['email'].replace('@', ' at ')} noch aktuell?"
    )

    state["history"].append({"role": "assistant", "content": f"Gesagt: {greeting}"})

    public_url = os.environ["PUBLIC_URL"].rstrip("/")
    return Response(
        twiml_response(greeting, gather_action=f"{public_url}/call/respond"),
        mimetype="text/xml",
    )


@app.route("/call/respond", methods=["POST"])
def call_respond():
    """Twilio schickt die Sprachantwort der Person hierher."""
    call_sid = request.form.get("CallSid", "")
    speech_result = request.form.get("SpeechResult", "").strip()
    log.info("Antwort empfangen [%s]: %s", call_sid, speech_result)

    if call_sid not in active_calls:
        vr = VoiceResponse()
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    state = active_calls[call_sid]
    contact = state["contact"]

    if not speech_result:
        return Response(
            twiml_response("Ich habe Sie leider nicht verstanden. Auf Wiederhören.", end=True),
            mimetype="text/xml",
        )

    result = ask_claude(contact, state["history"], speech_result)
    action = result.get("next_action", "end")
    reply_text = result.get("reply_text", "Auf Wiederhören.")

    public_url = os.environ["PUBLIC_URL"].rstrip("/")

    if action == "confirm":
        state["status"] = "confirmed"
        state["person_still_there"] = True
        save_result(call_sid, state)
        return Response(twiml_response(reply_text, end=True), mimetype="text/xml")

    elif action == "note_left":
        state["status"] = "person_left"
        state["person_still_there"] = False
        save_result(call_sid, state)
        return Response(twiml_response(reply_text, end=True), mimetype="text/xml")

    elif action == "ask_email":
        state["status"] = "awaiting_email"
        return Response(
            twiml_response(reply_text, gather_action=f"{public_url}/call/new_email"),
            mimetype="text/xml",
        )

    elif action == "retry":
        return Response(
            twiml_response(reply_text, gather_action=f"{public_url}/call/respond"),
            mimetype="text/xml",
        )

    else:  # end
        state["status"] = "ended"
        save_result(call_sid, state)
        return Response(twiml_response(reply_text, end=True), mimetype="text/xml")


@app.route("/call/new_email", methods=["POST"])
def call_new_email():
    """Person nennt die korrekte E-Mail-Adresse."""
    call_sid = request.form.get("CallSid", "")
    speech_result = request.form.get("SpeechResult", "").strip()
    log.info("Neue E-Mail empfangen [%s]: %s", call_sid, speech_result)

    if call_sid not in active_calls:
        vr = VoiceResponse()
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    state = active_calls[call_sid]
    state["status"] = "email_corrected"
    state["correct_email"] = speech_result
    state["person_still_there"] = True
    save_result(call_sid, state)

    reply = (
        f"Vielen Dank! Ich habe {speech_result} notiert. "
        f"Wir werden das in unserem System aktualisieren. Auf Wiederhören!"
    )
    return Response(twiml_response(reply, end=True), mimetype="text/xml")


@app.route("/call/status", methods=["POST"])
def call_status():
    """Twilio Status-Callback (angerufen, nicht abgenommen, etc.)."""
    call_sid = request.form.get("CallSid", "")
    call_status = request.form.get("CallStatus", "")
    log.info("Status-Update [%s]: %s", call_sid, call_status)

    if call_sid in active_calls and call_status in ("no-answer", "busy", "failed", "canceled"):
        state = active_calls[call_sid]
        state["status"] = call_status
        save_result(call_sid, state)

    return Response("OK", status=200)


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "active_calls": len(active_calls)}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
