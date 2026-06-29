import os
import time
import logging
import threading
import requests
from flask import Flask, request
from twilio.rest import Client
from twilio.request_validator import RequestValidator

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ─── Lock global — protege SOLO accesos a dicts, nunca I/O ───────────────────
data_lock = threading.Lock()

# ─── Credenciales ─────────────────────────────────────────────────────────────
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUM = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
MY_PERSONAL_NUMBER  = os.environ.get("MY_PERSONAL_NUMBER")
VALIDATE_TWILIO     = os.environ.get("VALIDATE_TWILIO", "false").lower() == "true"

if not GEMINI_API_KEY:
    raise ValueError("Error Crítico: Falta GEMINI_API_KEY en Railway")

# ─── Almacenes en memoria ─────────────────────────────────────────────────────
conversations:  dict = {}
muted_contacts: dict = {}

SESSION_TIMEOUT_SECONDS  = 60 * 60 * 4
MUTE_DURATION_SECONDS    = 60 * 60 * 4
MAX_HISTORY_MESSAGES     = 20
CLEANUP_INTERVAL_SECONDS = 60 * 60  # cada hora

ESCALATION_PHRASE  = "Permíteme un momento, voy a corroborar en el sistema."
ESCALATION_TRIGGER = "corroborar en el sistema"

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
Eres el asistente virtual inteligente de TjCCTV, un negocio especializado en la
consultoría, venta e instalación de sistemas de seguridad con cámaras (CCTV)
ubicado en Tijuana, Baja California.

Tu objetivo es atender de forma amable, profesional, fluida y directa a los
clientes por WhatsApp.

════════════════════════════════════════
REGLAS OBLIGATORIAS DE OPERACIÓN
════════════════════════════════════════

1. ESPECIALIZACIÓN ESTRICTA
   Solo manejamos sistemas de CCTV (cámaras de seguridad). NO instalamos alarmas
   de ningún tipo. Si el cliente pregunta por alarmas, acláraselo amablemente y
   orienta la plática hacia cámaras.

2. OBJETIVO DEL CHAT
   Ayudar al cliente a identificar:
   - Cuántas cámaras necesita aproximadamente
   - Tipo de espacio (casa habitación, negocio, terreno, bodega, etc.)
   - Si requiere visión nocturna, exterior, interior, o ambas
   - Su nombre y número de contacto
   ...para que José Luis pueda realizar una cotización formal y sin costo.

3. FLUJO RECOMENDADO
   a) Saluda con amabilidad y pregunta en qué puedes ayudar.
   b) Identifica el tipo de inmueble y número aproximado de cámaras.
   c) Pregunta si necesita cámaras interiores, exteriores o mixtas.
   d) Solicita su nombre y número de WhatsApp para el registro.
   e) Confirma que en breve se le enviará su cotización.

4. TONO Y FORMATO
   - Respuestas breves, directas y profesionales. Cero adornos ni excesos.
   - Párrafos cortos de 2 o 3 líneas para lectura fácil en móvil.
   - Trato amable pero eficiente al estilo fronterizo.

5. ESCALACIÓN DE DUDAS TÉCNICAS O COMPLEJAS
   Si el prospecto hace una pregunta técnica o compleja que te supere, pide un
   precio exacto imposible de calcular sin ver el lugar, o pide hablar directo
   con una persona:
   - PROHIBIDO mencionar que eres una inteligencia artificial o bot.
   - PROHIBIDO inventar datos, dar precios exactos o pasar contactos.
   - Tu única respuesta permitida para cerrar tu intervención debe ser
     estrictamente esta frase:
     "Permíteme un momento, voy a corroborar en el sistema."

6. FUERA DE TEMA
   Si hablan de algo ajeno a seguridad electrónica:
   "Perdón, en este número solo puedo apoyarte con proyectos de cámaras de
   seguridad. ¿Te puedo orientar en algo de eso?"
""".strip()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _is_muted(user_id: str, now: float) -> bool:
    """Verificación atómica de mute. Limpia entradas expiradas en el mismo paso."""
    with data_lock:
        expiry = muted_contacts.get(user_id)
        if expiry is None:
            return False
        if now < expiry:
            return True
        del muted_contacts[user_id]
        logger.info(f"Silencio expirado y levantado para: {user_id}")
        return False


def _cleanup_expired_sessions():
    """
    Thread daemon que limpia sesiones y mutes expirados cada hora.
    FIX: se inicia al nivel del módulo para que Gunicorn también lo arranque.
    """
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        with data_lock:
            expired_sessions = [
                uid for uid, s in conversations.items()
                if (now - s["last_active"]) > SESSION_TIMEOUT_SECONDS
            ]
            for uid in expired_sessions:
                del conversations[uid]
            if expired_sessions:
                logger.info(f"Cleanup: {len(expired_sessions)} sesiones expiradas eliminadas.")

            expired_mutes = [
                uid for uid, expiry in muted_contacts.items()
                if now >= expiry
            ]
            for uid in expired_mutes:
                del muted_contacts[uid]
            if expired_mutes:
                logger.info(f"Cleanup: {len(expired_mutes)} mutes expirados eliminados.")


def _send_alert_worker(client_phone: str, last_msg: str):
    """Thread daemon — envía alerta a José Luis sin bloquear el webhook."""
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        alert_text = (
            "🚨 *LEAD COMPLEJO EN TJ-CCTV*\n\n"
            f"👤 *Cliente:* `{client_phone}`\n"
            f"💬 *Última pregunta:* \"{last_msg}\"\n\n"
            "🤖 _El bot entró en silencio por 4 horas. Entra a responderle._"
        )
        client.messages.create(
            from_=TWILIO_WHATSAPP_NUM,
            body=alert_text,
            to=MY_PERSONAL_NUMBER
        )
        logger.info(f"Alerta enviada a José Luis [{client_phone}].")
    except Exception as e:
        logger.error(f"Fallo al enviar alerta Twilio: {e}")


def escalate_to_human(user_id: str, incoming_msg: str, now: float):
    """Silencia el número y despacha la alerta en background."""
    with data_lock:
        muted_contacts[user_id] = now + MUTE_DURATION_SECONDS
    logger.info(f"Escalación humana activada para: {user_id}")
    if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and MY_PERSONAL_NUMBER:
        threading.Thread(
            target=_send_alert_worker,
            args=(user_id, incoming_msg),
            daemon=True
        ).start()
    else:
        logger.error("Faltan credenciales Twilio para enviar la alerta.")

# ─── Lógica de Gemini ─────────────────────────────────────────────────────────

def get_gemini_response(user_id: str, incoming_msg: str) -> str:
    now = time.time()

    if _is_muted(user_id, now):
        return ""

    with data_lock:
        session = conversations.get(user_id)

    if session and (now - session["last_active"]) > SESSION_TIMEOUT_SECONDS:
        session = None
    if session is None:
        session = {"history": [], "last_active": now}

    # Copia local — la llamada HTTP ocurre FUERA de cualquier lock
    snapshot = list(session["history"])
    contents = snapshot + [{"role": "user", "parts": [{"text": incoming_msg}]}]

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=11,
        )
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates")
        if not candidates:
            block_reason = data.get("promptFeedback", {}).get("blockReason", "desconocida")
            logger.warning(f"Gemini bloqueó respuesta [{user_id}]. Razón: {block_reason}")
            return "Lo siento, no pude procesar esa solicitud. ¿Podrías reformularla?"

        bot_text = candidates[0]["content"]["parts"][0]["text"].strip()

        if (bot_text.lower().startswith("permíteme") and
                ESCALATION_TRIGGER in bot_text.lower()):
            escalate_to_human(user_id, incoming_msg, now)

        # Merge aditivo — el Hilo 2 nunca borra el trabajo del Hilo 1
        with data_lock:
            current_session = conversations.get(user_id, {"history": [], "last_active": now})
            current_history = current_session.get("history", [])
            merged_history  = current_history + [
                {"role": "user",  "parts": [{"text": incoming_msg}]},
                {"role": "model", "parts": [{"text": bot_text}]},
            ]
            current_session["history"]     = merged_history[-MAX_HISTORY_MESSAGES:]
            current_session["last_active"] = now
            conversations[user_id]         = current_session

        return bot_text

    except requests.exceptions.Timeout:
        logger.error(f"Timeout con Gemini para {user_id}.")
        escalate_to_human(user_id, incoming_msg, now)
        return ESCALATION_PHRASE

    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        logger.error(f"HTTP {status} de Gemini para {user_id}: {e}")
        escalate_to_human(user_id, incoming_msg, now)
        if status == 429:
            return "Estamos recibiendo muchas consultas en este momento. Por favor espera un par de minutos y vuelve a escribir."
        return ESCALATION_PHRASE

    except Exception as e:
        logger.error(f"Error inesperado [{user_id}]: {e}")
        escalate_to_human(user_id, incoming_msg, now)
        return ESCALATION_PHRASE

# ─── Webhook ──────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    if VALIDATE_TWILIO:
        validator  = RequestValidator(TWILIO_AUTH_TOKEN)
        scheme     = request.headers.get("X-Forwarded-Proto", "https")
        raw_host   = request.headers.get("X-Forwarded-Host", request.host)
        host       = raw_host.split(',')[0].split(':')[0].strip()
        public_url = f"{scheme}://{host}{request.path}"
        signature  = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(public_url, request.form.to_dict(), signature):
            logger.warning("Firma Twilio inválida — petición rechazada.")
            return "Forbidden", 403

    incoming_msg = request.values.get("Body", "").strip()
    from_number  = request.values.get("From", "").strip()

    if not from_number:
        logger.warning("Petición sin campo 'From'. Ignorada.")
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    if not incoming_msg:
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    now = time.time()

    if _is_muted(from_number, now):
        logger.info(f"Webhook: {from_number} en silencio, mensaje ignorado.")
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    bot_response = get_gemini_response(from_number, incoming_msg)

    if not bot_response:
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    safe_response = (
        bot_response
        .replace("&",  "&amp;")
        .replace("<",  "&lt;")
        .replace(">",  "&gt;")
        .replace('"',  "&quot;")
        .replace("'",  "&apos;")
    )

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"    <Message>{safe_response}</Message>\n"
        "</Response>"
    )
    return twiml, 200, {"Content-Type": "text/xml"}

# ─── Salud ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    now = time.time()
    with data_lock:
        sessions_vivas = sum(
            1 for s in conversations.values()
            if (now - s["last_active"]) < SESSION_TIMEOUT_SECONDS
        )
        muted_activos = sum(
            1 for expiry in muted_contacts.values()
            if now < expiry
        )
    return {
        "status":         "online",
        "sessions_vivas": sessions_vivas,
        "muted_activos":  muted_activos,
    }, 200

# ─── Inicio del thread de cleanup al nivel del módulo ─────────────────────────
# FIX: fuera del bloque if __name__ == "__main__" para que Gunicorn también
# lo arranque. Con __main__ solo funcionaría con `python main.py`, no en prod.
_cleanup_thread = threading.Thread(target=_cleanup_expired_sessions, daemon=True)
_cleanup_thread.start()
logger.info("Thread de cleanup iniciado.")

# ─── Entry point (desarrollo local) ──────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
