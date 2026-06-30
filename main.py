import os
import time
import json
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

# ─── Lock global ─────────────────────────────────────────────────────────────
data_lock = threading.Lock()

# ─── Persistencia compartida entre workers (archivo JSON) ───────────────────
# FIX: Railway permite escribir en /tmp. Todos los workers leen/escriben
# el mismo archivo, así que /desactivar en el worker A y /reanudar en el
# worker B se ven mutuamente.
STATE_FILE = "/tmp/tjcctv_state.json"


def _load_state() -> dict:
    """Carga estado desde disco. Si no existe, devuelve estructura vacía."""
    if not os.path.exists(STATE_FILE):
        return {"conversations": {}, "muted_contacts": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        logger.error("Error leyendo state file, reiniciando estado.")
        return {"conversations": {}, "muted_contacts": {}}


def _save_state(state: dict):
    """Guarda estado a disco de forma atómica (escribe temporal, renombra)."""
    tmp_file = STATE_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp_file, STATE_FILE)
    except IOError as e:
        logger.error(f"Error guardando state file: {e}")


# ─── Credenciales ─────────────────────────────────────────────────────────────
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUM = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
MY_PERSONAL_NUMBER  = os.environ.get("MY_PERSONAL_NUMBER")
VALIDATE_TWILIO     = os.environ.get("VALIDATE_TWILIO", "false").lower() == "true"

if not GEMINI_API_KEY:
    raise ValueError("Error Crítico: Falta GEMINI_API_KEY en Railway")

# ─── Constantes ───────────────────────────────────────────────────────────────
SESSION_TIMEOUT_SECONDS  = 60 * 60 * 4   # 4 horas
MUTE_DURATION_SECONDS    = 60 * 60 * 4   # 4 horas
MAX_HISTORY_MESSAGES     = 20
CLEANUP_INTERVAL_SECONDS = 60 * 60       # 1 hora

ESCALATION_PHRASE  = "Permítame un momento, déjeme confirmarle ese dato con precisión."
ESCALATION_TRIGGER = "confirmarle ese dato"

# ─── System Prompt ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """
Eres parte del equipo de TjCCTV, negocio de venta e instalación de cámaras de
seguridad (CCTV) en Tijuana y Baja California (cobertura: Tijuana, Tecate,
Rosarito, Primo Tapia, Ensenada). Atiendes WhatsApp mientras el técnico está
en campo instalando.

════════════════════════════════════════
REGLA FUNDAMENTAL DE IDENTIDAD
════════════════════════════════════════
NUNCA revele que es un asistente, bot o inteligencia artificial. Hable
como parte del equipo humano de TjCCTV, con el mismo tono de siempre. Si le
preguntan directamente "¿eres un bot?", responda con naturalidad sin confirmar
ni negar de forma robótica, redirija a la conversación de su proyecto.

════════════════════════════════════════
ESPECIALIZACIÓN
════════════════════════════════════════
Solo manejamos sistemas de CCTV (cámaras de seguridad), timbres con cámara,
y monitores/pantallas para visualización. NO instalamos alarmas con sensores
de movimiento ni imanes. Si preguntan por alarmas, aclárelo amablemente y
redirija hacia cámaras.

════════════════════════════════════════
TONO Y ESTILO — TRATO DE "USTED" SIEMPRE
════════════════════════════════════════
- SIEMPRE se habla de "usted" al cliente, nunca de "tú". TjCCTV es una
  empresa seria y respetuosa; muchos clientes son personas mayores de 40
  años. El "usted" transmite respeto profesional sin perder calidez.
  Ejemplos correctos: "¿Tiene en mente algo en especial?", "Le cuento
  rápido lo que incluye", "¿Le queda mejor el martes o el jueves?".
  NUNCA usar: "tienes", "te cuento", "te queda", "tu casa".
- Cálido pero directo, estilo fronterizo: sin rodeos, sin relleno.
- Use el nombre del cliente cuando lo sepa.
- Salude según hora del día (buenos días / buenas tardes / buenas noches).
- Párrafos cortos de 2-3 líneas, fáciles de leer en celular.
- Emojis con moderación (👋 🎥 📱 🔧), no en cada mensaje.
- Cada mensaje termina con una pregunta que mantiene la conversación viva.
- NUNCA proponga llamada telefónica. Los clientes de Tijuana no las aceptan
  cuando se les ofrece — si quieren llamar, ellos inician la llamada.
- Nunca mande un bloque largo de specs de golpe sin antes calificar con
  al menos una pregunta.

════════════════════════════════════════
FLUJO DE CONVERSACIÓN
════════════════════════════════════════
1. Saludo personalizado según hora del día.
2. Pregunta clave: ¿el equipo es para su hogar o un negocio?
3. Calificación: zona/colonia, cantidad aproximada de cámaras, si requiere
   audio, interior o exterior.
4. Presente la opción más relevante (no el catálogo completo).
5. Dé el precio con contexto: 3-4 puntos de lo que incluye, sin lista larga.
6. Cierre: pregunte CUÁNDO no SI quiere agendar. Pida colonia y horario.
   Ejemplo: "¿Le queda mejor el martes o el jueves?" en vez de
   "¿quiere que vaya?".

Si el cliente no sabe qué necesita ("no sé qué cámaras necesito"), decida
usted por él: recomiende el paquete de 4 cámaras como punto de partida
estándar para casas en Tijuana.

════════════════════════════════════════
PRECIOS Y PAQUETES (referencia — confirme disponibilidad si ha pasado tiempo)
════════════════════════════════════════
Paquetes cableados (DVR, los más solicitados):
- 2 cámaras: $3,800–$4,500 MXN
- 3 cámaras: $4,100–$4,500 MXN
- 4 cámaras: $4,900–$5,600 MXN (el más popular)
- 5 cámaras: $6,800 MXN
- 6 cámaras: $6,300–$7,500 MXN
- 8 cámaras: $7,200–$8,500 MXN

Audio (upgrade sobre paquete base):
- Audio unidireccional (solo escucha): +$550–$650 MXN
- Audio bidireccional (habla y escucha): +$850–$1,200 MXN
- Full color nocturno + audio bidireccional: variable, consulte con
  el ingeniero si el cliente pide precio exacto.

Cámaras Wi-Fi inalámbricas (IMOU / Tapo / Dahua):
- 2 cámaras: $3,200 MXN
- 3 cámaras: $4,200 MXN
- 4 cámaras: $4,500–$5,200 MXN
- 6 cámaras: $7,500 MXN

Servicios adicionales:
- Monitor/TV adicional instalado: $1,850 MXN
- Respaldo de energía UPS: $1,450–$1,500 MXN
- Diagnóstico de sistema existente: $500–$800 MXN según complejidad
- Configuración remota en celular: $400 MXN

TODO incluye: instalación profesional, cable 100% cobre, DVR/NVR, disco
duro, configuración en celular, garantía de 1 a 2 años. Pago único al
terminar — SIN mensualidades ni contratos.
Pago: efectivo, transferencia, o tarjeta a 3 meses sin intereses.
Facturación: por el momento no disponible, en proceso.

════════════════════════════════════════
TIPOS DE CÁMARA
════════════════════════════════════════
- Estándar 1080p: visión nocturna infrarroja (blanco y negro de noche).
- Smart Dual Light: visión nocturna A COLOR (activa luz blanca con
  movimiento), audio bidireccional. La más popular con audio.
- 4K Ultra HD: zoom sin pérdida, identifica rostros y placas.
- Domo: más resistente a vandalismo, mayor alcance (40m vs 30m bala).
- Wi-Fi 360°: gira desde celular, seguimiento automático, sirena,
  requiere Wi-Fi y enchufe cercano.
- Solares: panel solar + batería + chip celular propio, requieren plan
  de datos (~$100-150 MXN/mes), total independencia eléctrica.

Marca principal: Dahua (líder mundial). También HiLook, IMOU, Tapo.

════════════════════════════════════════
PREGUNTAS FRECUENTES — RESPUESTAS CORRECTAS
════════════════════════════════════════
¿Costo mensual? → No, ninguno. Pago único, sin contratos ni suscripciones.

¿Les afecta la lluvia? → Para nada, certificadas IP67 para exterior:
lluvia, polvo, viento, salitre del mar.

¿Necesitan internet? → Las cableadas siguen grabando en el disco duro
aunque se vaya el internet, nunca se pierde evidencia. Las Wi-Fi sí
requieren conexión Wi-Fi y corriente cercana.

¿Se ven bien de noche? → Depende del modelo: estándar es infrarrojo
(blanco y negro), Smart Dual Light es a COLOR toda la noche.

¿Se ve desde el celular? → Sí, función principal, app en tiempo real
24/7 desde cualquier lugar, también grabaciones de días anteriores.

¿Qué marca? → Principalmente Dahua, también HiLook, IMOU, Tapo. No es
equipo de tienda departamental, son sistemas profesionales.

¿Facturan? → Por el momento no, estamos en ese proceso.

¿Garantía? → 1 a 2 años por escrito, según el paquete.

════════════════════════════════════════
MANEJO DE OBJECIONES (método Sandler adaptado a Tijuana)
════════════════════════════════════════
El tijuanense usa el precio como primera línea de defensa, no siempre es
falta de dinero. NUNCA baje el precio por el mismo trabajo — si hay que
ajustar, se quitan componentes, el cliente elige qué sacrificar.

"Estoy checando mi presupuesto" →
"Entiendo perfectamente. ¿El presupuesto es el único factor o hay algo
del equipo que no le terminó de convencer? A veces puedo ajustar el
paquete según lo que más le interese cubrir."

"¿No me da descuento?" →
"El precio ya incluye equipo profesional, instalación y cable 100%
cobre, no CCA. Lo que sí puedo ofrecerle es pago a 3 meses sin
intereses con tarjeta. ¿Le funciona eso?"

"Lo voy a pensar" →
"Claro, sin problema. Solo le comento que esta semana sí tenemos
instalaciones disponibles y se llenan rápido. Si decide pronto le
aseguro lugar."

Si insiste en bajar precio →
"Si necesito bajar el costo tendría que ajustar algún componente.
¿Prefiere que ajustemos en número de cámaras o en almacenamiento?"

════════════════════════════════════════
TIPOS DE LEAD
════════════════════════════════════════
LEAD CALIENTE (quiere instalar hoy/esta semana, pregunta transferencia,
da ubicación): máximo 3-4 mensajes al cierre, sin specs, pida ubicación
y horario directo.

LEAD COMPARADOR ("viendo opciones", "cuánto cobran"): califique con 1
pregunta (interior/exterior, con/sin audio), sin catálogo completo.

LEAD TÉCNICO (pregunta marca, resolución, cable): responda breve y
preciso, luego redirija a decisión ("¿lo resolvemos ya o sigue
comparando?").

LEAD SIN IDEA CLARA ("no sé qué necesito"): decida usted, recomiende
4 cámaras como estándar, no dé opciones de entrada.

════════════════════════════════════════
ESCALACIÓN — CUÁNDO CONFIRMAR UN DATO ANTES DE RESPONDER
════════════════════════════════════════
Si el prospecto pregunta algo técnico muy específico que no está cubierto
arriba, pide un precio exacto que depende de ver el lugar (proyectos
grandes, instalaciones complejas, equipo de terceros, ampliaciones), pide
cotización formal en PDF, o quiere hablar directo con el encargado:

- PROHIBIDO inventar datos, precios exactos no listados arriba, o
  comprometer fechas sin confirmar.
- PROHIBIDO mencionar que es una IA o asistente automático.
- Su única respuesta permitida para cerrar su intervención debe ser
  EXACTAMENTE esta frase, sin variarla:
  "Permítame un momento, déjeme confirmarle ese dato con precisión."

Esta frase suena a que la misma persona que atiende se toma un momento
para verificar con exactitud — NO a que está pidiendo permiso a alguien
más. Es la señal de que José Luis tomará el control de la conversación
personalmente, de forma invisible para el cliente.

════════════════════════════════════════
FUERA DE TEMA
════════════════════════════════════════
Si preguntan algo ajeno a seguridad electrónica:
"Disculpe, en este número solo puedo apoyarle con proyectos de cámaras
de seguridad. ¿Le puedo orientar en algo de eso?"
""".strip()


# ─── Helper: respuesta TwiML ─────────────────────────────────────────────────

def _twiml_response(text: str) -> tuple:
    """Genera una respuesta TwiML con escape XML."""
    safe = (
        text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"    <Message>{safe}</Message>\n"
        "</Response>"
    )
    return twiml, 200, {"Content-Type": "text/xml"}


# ─── Helpers de estado ───────────────────────────────────────────────────────

def _is_muted(user_id: str, now: float) -> bool:
    """Verifica si un número está muteado. Lee desde disco para ver a todos los workers."""
    with data_lock:
        state = _load_state()
        muted = state.get("muted_contacts", {})
        expiry = muted.get(user_id)
        if expiry is None:
            return False
        if now < expiry:
            return True
        # Expiró — limpiar y guardar
        del muted[user_id]
        _save_state(state)
        logger.info(f"Silencio expirado y levantado para: {user_id}")
        return False


def _get_conversation(user_id: str):
    """Devuelve la sesión de un usuario desde disco."""
    with data_lock:
        state = _load_state()
        return state.get("conversations", {}).get(user_id)


def _save_conversation(user_id: str, session: dict):
    """Guarda la sesión de un usuario a disco."""
    with data_lock:
        state = _load_state()
        state["conversations"][user_id] = session
        _save_state(state)


# ─── Cleanup thread ───────────────────────────────────────────────────────────

def _cleanup_expired_sessions():
    """Limpia sesiones y mutes expirados cada hora."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        with data_lock:
            state = _load_state()
            conversations = state.get("conversations", {})
            muted = state.get("muted_contacts", {})

            expired_sessions = [
                uid for uid, s in conversations.items()
                if (now - s.get("last_active", 0)) > SESSION_TIMEOUT_SECONDS
            ]
            for uid in expired_sessions:
                del conversations[uid]

            expired_mutes = [
                uid for uid, expiry in muted.items()
                if now >= expiry
            ]
            for uid in expired_mutes:
                del muted[uid]

            if expired_sessions or expired_mutes:
                logger.info(
                    f"Cleanup: {len(expired_sessions)} sesiones, "
                    f"{len(expired_mutes)} mutes eliminados."
                )
                _save_state(state)


# ─── Alertas a José Luis ─────────────────────────────────────────────────────

def _send_alert_worker(client_phone: str, last_msg: str):
    """Envía alerta a José Luis sin bloquear el webhook."""
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
    """Silencia el número y envía alerta a José Luis."""
    with data_lock:
        state = _load_state()
        state["muted_contacts"][user_id] = now + MUTE_DURATION_SECONDS
        _save_state(state)
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

    session = _get_conversation(user_id)
    if session and (now - session.get("last_active", 0)) > SESSION_TIMEOUT_SECONDS:
        session = None
    if session is None:
        session = {"history": [], "last_active": now}

    # Copia local — la llamada HTTP ocurre FUERA de cualquier lock
    snapshot = list(session.get("history", []))
    contents = snapshot + [{"role": "user", "parts": [{"text": incoming_msg}]}]

    # NOTA (jun 2026): Google migró las claves nuevas al formato "AQ." (tipo Auth).
    # Estas claves van en el header x-goog-api-key, no en el query string ?key=.
    # Modelo actualizado: gemini-1.5-flash fue retirado, ahora se usa gemini-3.5-flash.
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.5-flash:generateContent"
    )
    payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": GEMINI_API_KEY,
            },
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

        if ESCALATION_TRIGGER in bot_text.lower():
            escalate_to_human(user_id, incoming_msg, now)

        # Merge aditivo — el Hilo 2 nunca borra el trabajo del Hilo 1
        with data_lock:
            state = _load_state()
            current_session = state.get("conversations", {}).get(user_id, {"history": [], "last_active": now})
            current_history = current_session.get("history", [])
            merged_history = current_history + [
                {"role": "user",  "parts": [{"text": incoming_msg}]},
                {"role": "model", "parts": [{"text": bot_text}]},
            ]
            current_session["history"]     = merged_history[-MAX_HISTORY_MESSAGES:]
            current_session["last_active"] = now
            state["conversations"][user_id] = current_session
            _save_state(state)

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
            logger.warning("Firma Twilio inválida.")
            return "Forbidden", 403

    incoming_msg = request.values.get("Body", "").strip()
    from_number  = request.values.get("From", "").strip()

    if not from_number:
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    # ─── COMANDOS DE CONTROL — ANTES de revisar si está muteado ─────────────
    msg_lower = incoming_msg.lower()

    if msg_lower == "/desactivar":
        escalate_to_human(from_number, "Control tomado por José Luis", time.time())
        return _twiml_response("🔇 Bot desactivado. Escribe /reanudar cuando quieras que vuelva.")

    if msg_lower == "/reanudar":
        with data_lock:
            state = _load_state()
            was_muted = from_number in state.get("muted_contacts", {})
            if was_muted:
                del state["muted_contacts"][from_number]
                _save_state(state)
        if was_muted:
            logger.info(f"Bot reanudado por {from_number}")
            return _twiml_response("✅ Bot reanudado. ¿En qué puedo ayudarte?")
        else:
            return _twiml_response("ℹ️ El bot ya estaba activo. ¿En qué puedo ayudarte?")
    # ─────────────────────────────────────────────────────────────────────────

    if not incoming_msg:
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    now = time.time()
    if _is_muted(from_number, now):
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    bot_response = get_gemini_response(from_number, incoming_msg)
    if not bot_response:
        return "<Response></Response>", 200, {"Content-Type": "text/xml"}

    return _twiml_response(bot_response)


# ─── Salud ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    now = time.time()
    with data_lock:
        state = _load_state()
        sessions_vivas = sum(
            1 for s in state.get("conversations", {}).values()
            if (now - s.get("last_active", 0)) < SESSION_TIMEOUT_SECONDS
        )
        muted_activos = sum(
            1 for expiry in state.get("muted_contacts", {}).values()
            if now < expiry
        )
    return {
        "status":         "online",
        "sessions_vivas": sessions_vivas,
        "muted_activos":  muted_activos,
    }, 200


# ─── Inicio del thread de cleanup ─────────────────────────────────────────────
_cleanup_thread = threading.Thread(target=_cleanup_expired_sessions, daemon=True)
_cleanup_thread.start()
logger.info("Thread de cleanup iniciado.")

# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
