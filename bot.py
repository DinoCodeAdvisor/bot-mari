import os
import io
import base64
import re
import json
from datetime import datetime, timedelta

from dotenv import load_dotenv
import openai
from PIL import Image

from google.oauth2 import service_account
from googleapiclient.discovery import build

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CALENDAR_ID = os.getenv("CALENDAR_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

openai.api_key = OPENAI_API_KEY

# Conversation states
IDLE, WAITING_INE, WAITING_DATE_TIME = range(3)

# Helper Functions (extracted from original app.py)
def check_cita_intent(text: str) -> bool:
    """Verifica si el usuario quiere agendar una cita."""
    keywords = [
        "cita", "reservar", "reserva", "agendar", "agenda",
        "quiero una cita", "hacer una cita", "necesito cita",
        "hola", "buenos días", "buenas", "qué tal", "buen dia"
    ]
    text_lower = text.lower()
    return any(k in text_lower for k in keywords)

def validate_and_extract_ine(image_b64: str) -> str:
    """Usa GPT-4o Vision para validar si es un INE y extraer datos."""
    prompt = '''
Analiza esta imagen y determina si es una credencial de elector (INE) de México.

Responde SOLO en formato JSON válido, sin texto adicional:

Si NO es una INE válida:
{ "validate": false }

Si ES una INE válida:
{
  "validate": true,
  "nombre": "NOMBRE COMPLETO",
  "direccion": "DIRECCIÓN",
  "fecha_nacimiento": "DD/MM/AAAA",
  "curp": "CURP"
}
'''
    try:
        r = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                ]
            }],
            temperature=0,
            max_tokens=300
        )
        return r["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"❌ Error al validar INE: {e}")
        return '{"validate": false}'

def parse_date_time_with_gpt(text: str) -> str:
    """Usa GPT para parsear fechas y horas incluso si son confusas."""
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    current_time = now.strftime("%H:%M")

    dias_semana = {
        'monday': 'lunes', 'tuesday': 'martes', 'wednesday': 'miércoles',
        'thursday': 'jueves', 'friday': 'viernes', 'saturday': 'sábado', 'sunday': 'domingo'
    }
    current_day_english = now.strftime("%A").lower()
    current_day_spanish = dias_semana.get(current_day_english, current_day_english)

    prompt = f'''
Eres un extractor de fechas y horas experto en español.
Fecha y hora actual: {current_date} {current_time}
Día de la semana actual: {current_day_spanish} ({current_day_english})
Zona horaria: America/Mexico_City

Extrae la fecha y hora del siguiente mensaje: "{text}"

REGLAS IMPORTANTES PARA CALCULAR FECHAS:
1. "hoy" = {current_date}
2. "mañana" = {(now + timedelta(days=1)).strftime("%Y-%m-%d")}
3. "pasado mañana" = {(now + timedelta(days=2)).strftime("%Y-%m-%d")}
4. "en X días" o "dentro de X días" = suma exactamente X días a la fecha actual
5. "en X semanas" = suma X*7 días a la fecha actual
6. "el próximo lunes/martes/miércoles/jueves/viernes/sábado/domingo" = siguiente día de la semana
7. "el lunes/martes/etc que viene" = siguiente día de la semana

REGLAS IMPORTANTES PARA HORAS:
- "3 PM" o "3 de la tarde" = 15:00
- "3 AM" o "3 de la mañana" = 03:00
- "7 de la noche" = 19:00
- "10 de la mañana" = 10:00
- "12 del mediodía" o "12 PM" = 12:00
- Si NO menciona una hora ESPECÍFICA (como "3 PM", "15:00", etc.), falta hora
- "en la mañana", "en la tarde", "en la noche" NO son horas específicas

FORMATO DE RESPUESTA (SOLO JSON):
- Si tiene fecha y hora: {{"date": "YYYY-MM-DD", "time": "HH:MM", "missing": null}}
- Si falta hora: {{"date": "YYYY-MM-DD", "time": null, "missing": "hora"}}
- Si falta fecha: {{"date": null, "time": "HH:MM", "missing": "fecha"}}
- Si faltan ambos: {{"date": null, "time": null, "missing": "ambos"}}
- Si no entiende: {{"date": null, "time": null, "missing": "no_entendido"}}

Responde SOLO en formato JSON válido, sin texto adicional ni markdown.
'''
    try:
        r = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Responde solo en JSON válido, sin texto adicional ni markdown."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=150
        )
        return r["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"❌ Error al parsear fecha: {e}")
        return '{"date": null, "time": null, "missing": "error"}'

def validate_date_time(date_str: str, time_str: str) -> tuple[bool, str | None, datetime | None]:
    """
    Valida que la fecha y hora sean válidas.
    Retorna: (is_valid, error_message, datetime_object)
    """
    try:
        if not date_str or not time_str:
            return True, None, None

        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        now = datetime.now()

        if dt < now - timedelta(minutes=1):
            return False, "❌ La fecha y hora no pueden ser en el pasado. Por favor elige una fecha futura.", None

        max_future = now + timedelta(days=30)
        if dt > max_future:
            return False, "❌ Solo puedo agendar citas hasta 30 días en el futuro. Por favor elige una fecha más cercana.", None

        hour = dt.hour
        if hour < 10 or hour >= 20:
            return False, "❌ Nuestro horario de atención es de 10:00 AM a 8:00 PM. Por favor elige una hora dentro de este horario.", None

        return True, None, dt

    except ValueError as e:
        return False, f"❌ Formato de fecha/hora inválido: {str(e)}", None

def create_calendar_event(nombre: str, dt: datetime) -> tuple[bool, str | None]:
    """Crea un evento en Google Calendar en la fecha y hora especificadas."""
    try:
        creds = service_account.Credentials.from_service_account_file(
            "credentials.json",
            scopes=["https://www.googleapis.com/auth/calendar"]
        )

        service = build("calendar", "v3", credentials=creds)

        event = {
            "summary": f"Cita - {nombre}",
            "description": f"Cita agendada vía Telegram",
            "start": {
                "dateTime": dt.isoformat(),
                "timeZone": "America/Mexico_City"
            },
            "end": {
                "dateTime": (dt + timedelta(hours=1)).isoformat(),
                "timeZone": "America/Mexico_City"
            },
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 1440},  # 1 día antes
                    {"method": "popup", "minutes": 60}     # 1 hora antes
                ]
            }
        }

        created_event = service.events().insert(
            calendarId=CALENDAR_ID,
            body=event
        ).execute()

        print("✅ Evento creado correctamente")
        print("ID:", created_event.get("id"))
        print("Link:", created_event.get("htmlLink"))
        return True, created_event.get("htmlLink")

    except Exception as e:
        print("❌ Error al crear evento:", e)
        return False, None

def format_date_spanish(dt: datetime) -> str:
    """Formatea la fecha en español."""
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    dias_semana = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo"
    }
    dia = dt.day
    mes = meses[dt.month]
    año = dt.year
    dia_semana = dias_semana[dt.weekday()]
    try:
        hora = dt.strftime("%I:%M %p")
        return f"{dia_semana}, {dia} de {mes} de {año} a las {hora}"
    except:
        return f"{dia_semana}, {dia} de {mes} de {año}"

def format_only_date_spanish(date_str: str) -> str:
    """Formatea solo la fecha en español."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    dias_semana = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo"
    }
    dia = dt.day
    mes = meses[dt.month]
    año = dt.year
    dia_semana = dias_semana[dt.weekday()]
    return f"{dia_semana}, {dia} de {mes} de {año}"


# Telegram Handler Functions
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends a message when the command /start is issued."""
    await update.message.reply_text(
        "Hola 👋 ¿En qué puedo ayudarte?\n\n"
        "Escribe 'cita' para agendar una cita."
    )
    context.user_data["state"] = IDLE
    return IDLE

async def request_cita(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initiates the appointment scheduling process."""
    if context.user_data.get("state") == IDLE:
        await update.message.reply_text(
            "¡Hola! 👋 Para agendar tu cita, necesito verificar tu identidad.\n\n"
        "📷 Por favor, envíame una foto de tu INE (credencial de elector)."
        )
        context.user_data["state"] = WAITING_INE
        context.user_data["nombre"] = None
        context.user_data["fecha_parcial"] = None
        context.user_data["hora_parcial"] = None
        return WAITING_INE

    # If not in idle state, just ignore or re-prompt if needed
    return IDLE # This might need adjustment based on desired behavior

async def receive_ine_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles photo messages when in WAITING_INE state."""
    await update.message.reply_text("🔍 Verificando tu INE...")

    # Get the largest photo available
    photo_file = await update.message.photo[-1].get_file()

    # Download the photo content
    photo_bytes = io.BytesIO()
    await photo_file.download_to_memory(photo_bytes)
    photo_bytes.seek(0) # Reset buffer position to the beginning

    # Convert to base64
    img = Image.open(photo_bytes)
    buff = io.BytesIO()
    img.save(buff, format="JPEG")
    b64 = base64.b64encode(buff.getvalue()).decode()

    # Validate INE with GPT-4o
    result = validate_and_extract_ine(b64)

    try:
        result_clean = result
        if '```json' in result_clean:
            result_clean = result_clean.split('```json')[1].split('```')[0]
        elif '```' in result_clean:
            result_clean = result_clean.split('```')[1].split('```')[0]

        data = json.loads(result_clean.strip())
    except json.JSONDecodeError as e:
        print(f"❌ Error parseando INE: {e}")
        await update.message.reply_text("❌ Error al leer el INE. Asegúrate de que la foto sea clara.")
        return WAITING_INE # Stay in the same state

    if data.get("validate") is True:
        nombre = data.get("nombre", "Cliente")
        context.user_data["state"] = WAITING_DATE_TIME
        context.user_data["nombre"] = nombre
        await update.message.reply_text(
            f"✅ ¡INE verificado correctamente!\n"
            f"👤 Nombre: {nombre}\n\n"
            f"📅 ¿Para qué fecha y hora quieres tu cita?\n\n"
            f"Puedes escribirlo de forma natural:\n"
            f"• 'Mañana a las 3 PM'\n"
            f"• 'En 15 días a las 10 de la mañana'\n"
            f"• 'El próximo sábado a las 7 de la noche'"
        )
        return WAITING_DATE_TIME
    else:
        await update.message.reply_text(
            "❌ La imagen no parece ser un INE válido.\n\n"
            "Por favor, envía una foto clara del frente de tu INE."
        )
        return WAITING_INE # Stay in the same state

async def receive_date_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles text messages when in WAITING_DATE_TIME state."""
    text = update.message.text

    if context.user_data.get("state") != WAITING_DATE_TIME:
        # Should not happen with ConversationHandler, but as a safeguard
        await update.message.reply_text("Para agendar una cita, primero di 'cita'.")
        context.user_data["state"] = IDLE
        return IDLE

    await update.message.reply_text("🔍 Procesando...")
    result_json = parse_date_time_with_gpt(text)

    try:
        if '```json' in result_json:
            result_json = result_json.split('```json')[1].split('```')[0]
        elif '```' in result_json:
            result_json = result_json.split('```')[1].split('```')[0]

        parsed = json.loads(result_json.strip())
        missing = parsed.get("missing")
        date_str = parsed.get("date")
        time_str = parsed.get("time")

        nombre = context.user_data.get("nombre", "Cliente")
        fecha_parcial = context.user_data.get("fecha_parcial")
        hora_parcial = context.user_data.get("hora_parcial")

        if missing == "no_entendido" or missing == "error":
            await update.message.reply_text(
                "❌ No pude entender lo que me dices. Por favor intenta de nuevo.\n\n"
                "Ejemplos:\n"
                "• 'Mañana a las 3 PM'\n"
                "• 'En 15 días a las 10 de la mañana'\n"
                "• 'El próximo sábado a las 7 de la noche'"
            )
            return WAITING_DATE_TIME

        if fecha_parcial and time_str:
            date_str = fecha_parcial
        if hora_parcial and date_str:
            time_str = hora_parcial

        if missing == "ambos" or (not date_str and not time_str):
            await update.message.reply_text(
                "❌ Necesito que me digas la fecha y la hora para tu cita.\n\n"
                "Por ejemplo:\n"
                "• 'El 25 de febrero a las 3 PM'\n"
                "• 'En 15 días a las 11 de la mañana'"
            )
            return WAITING_DATE_TIME

        if missing == "hora" or (date_str and not time_str):
            context.user_data["fecha_parcial"] = date_str
            fecha_formateada = format_only_date_spanish(date_str)
            await update.message.reply_text(
                f"📅 Entendí la fecha: {fecha_formateada}\n\n"
                f"⏰ ¿A qué hora te gustaría tu cita?\n\n"
                f"Nuestro horario es de 10:00 AM a 8:00 PM."
            )
            return WAITING_DATE_TIME

        if missing == "fecha" or (time_str and not date_str):
            context.user_data["hora_parcial"] = time_str
            await update.message.reply_text(
                f"⏰ Entendí la hora: {time_str}\n\n"
                f"📅 ¿Para qué fecha?\n\n"
                f"Puedes decir:\n"
                f"• 'Mañana'\n"
                f"• 'En 15 días'\n"
                f"• 'El próximo lunes'"
            )
            return WAITING_DATE_TIME

        is_valid, error_msg, dt = validate_date_time(date_str, time_str)

        if not is_valid:
            await update.message.reply_text(error_msg)
            return WAITING_DATE_TIME

        success, link = create_calendar_event(nombre, dt)

        if success:
            fecha_formateada = format_date_spanish(dt)
            await update.message.reply_text(
                f"🎉 ¡Cita confirmada!\n\n"
                f"📅 Fecha: {fecha_formateada}\n"
                f"👤 Nombre: {nombre}\n\n"
                f"✅ Se ha agendado en nuestro calendario.\n"
                f"📧 Recibirás recordatorios antes de tu cita.\n\n"
                f"¡Te esperamos! 😊"
            )
        else:
            await update.message.reply_text("❌ Hubo un error al agendar. Intenta de nuevo.")

        context.user_data.clear() # Clear all user data for this conversation
        context.user_data["state"] = IDLE # Reset state
        return IDLE

    except json.JSONDecodeError as e:
        print(f"❌ Error parseando JSON: {e}")
        await update.message.reply_text(
            "❌ No pude procesar la información. Intenta con otro formato.\n"
            "Ejemplo: 'Mañana a las 3 PM'"
        )
        return WAITING_DATE_TIME

async def fallback_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles messages that don't match any specific handler in the current state."""
    user_state = context.user_data.get("state")
    if user_state == IDLE:
        await update.message.reply_text(
            "Hola 👋 ¿En qué puedo ayudarte?\n\n"
        "Escribe 'cita' para agendar una cita."
        )
    else:
        # This could be a more specific message based on the state if needed
        await update.message.reply_text("No entiendo tu mensaje. Por favor, sigue las instrucciones o inicia de nuevo con /start.")
    return user_state # Remain in current state or transition to IDLE if preferred

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(
                filters.TEXT & ~filters.COMMAND & filters.Regex(
                    r"(?i)(cita|reservar|reserva|agendar|agenda|quiero una cita|hacer una cita|necesito cita|hola|buenos días|buenas|qué tal|buen dia)"
                ),
                request_cita,
            )
        ],
        states={
            IDLE: [
                 MessageHandler(
                    filters.TEXT & ~filters.COMMAND & filters.Regex(
                        r"(?i)(cita|reservar|reserva|agendar|agenda|quiero una cita|hacer una cita|necesito cita|hola|buenos días|buenas|qué tal|buen dia)"
                    ),
                    request_cita,
                ),
            ],
            WAITING_INE: [
                MessageHandler(filters.PHOTO, receive_ine_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_message), # Handle text if user is in photo state
            ],
            WAITING_DATE_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_date_time),
            ],
        },
        fallbacks=[
            CommandHandler("start", start), # Allows restarting conversation
            MessageHandler(filters.ALL, fallback_message) # General fallback for unhandled messages
        ],
    )

    application.add_handler(conv_handler)

    # Set up webhook
    if WEBHOOK_URL:
        # Note: The webhook_url_path should match the path configured in your Telegram Botfather webhook settings
        application.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", 8443)), # Default to 8443 or use env PORT
            url_path="/telegram",
            webhook_url=f"{WEBHOOK_URL}/telegram"
        )
        print(f"Bot started with webhook at {WEBHOOK_URL}/telegram")
    else:
        print("WEBHOOK_URL not set, falling back to long polling. This is not recommended for production.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
