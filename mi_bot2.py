import os, io, base64, re
from flask import Flask, request, jsonify
import requests
import openai
import json
from PIL import Image
from datetime import datetime, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

openai.api_key = "your api key"

TELEGRAM_TOKEN = "your bot token here"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ID del calendario de Google
CALENDAR_ID = "your google calenda id here"

app = Flask(__name__)

# Estados de usuario: {chat_id: {"state": "...", "nombre": "...", "fecha_parcial": "...", "hora_parcial": "..."}}
USER_STATES = {}

class TelegramBot:
    def send_message(self, chat_id, text):
        """Envía un mensaje de texto a Telegram."""
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text
        })

    def download_photo(self, file_id):
        """Descarga una foto de Telegram."""
        r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
        if not r.get("ok"):
            return None
        path = r["result"]["file_path"]
        return requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}").content

    def check_cita_intent(self, text):
        """Verifica si el usuario quiere agendar una cita."""
        keywords = [
            "cita", "reservar", "reserva", "agendar", "agenda",
            "quiero una cita", "hacer una cita", "necesito cita",
            "hola", "buenos días", "buenas", "qué tal", "buen dia"
        ]
        text_lower = text.lower()
        return any(k in text_lower for k in keywords)

    def validate_and_extract_ine(self, image_b64):
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

    def parse_date_time_with_gpt(self, text):
        """Usa GPT para parsear fechas y horas incluso si son confusas."""
        now = datetime.now()
        current_date = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M")

        # Días de la semana en español
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

    def validate_date_time(self, date_str, time_str):
        """
        Valida que la fecha y hora sean válidas.
        Retorna: (is_valid, error_message, datetime_object)
        """
        try:
            # Si falta fecha o hora, retornar sin error pero sin datetime
            if not date_str or not time_str:
                return True, None, None

            # Combinar fecha y hora
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            now = datetime.now()

            # 1. Verificar que no sea en el pasado (con margen de 1 minuto)
            if dt < now - timedelta(minutes=1):
                return False, "❌ La fecha y hora no pueden ser en el pasado. Por favor elige una fecha futura.", None

            # 2. Verificar que no sea más de 30 días en el futuro
            max_future = now + timedelta(days=30)
            if dt > max_future:
                return False, "❌ Solo puedo agendar citas hasta 30 días en el futuro. Por favor elige una fecha más cercana.", None

            # 3. Verificar horario hábil (10:00 - 20:00)
            hour = dt.hour
            if hour < 10 or hour >= 20:
                return False, "❌ Nuestro horario de atención es de 10:00 AM a 8:00 PM. Por favor elige una hora dentro de este horario.", None

            return True, None, dt

        except ValueError as e:
            return False, f"❌ Formato de fecha/hora inválido: {str(e)}", None

    def create_calendar_event(self, nombre, dt):
        """Crea un evento en Google Calendar en la fecha y hora especificadas."""
        try:
            creds = service_account.Credentials.from_service_account_file(
                "credentials.json",
                scopes=["https://www.googleapis.com/auth/calendar"]
            )

            service = build("calendar", "v3", credentials=creds)

            # Crear evento con la fecha y hora proporcionadas
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

    def format_date_spanish(self, dt):
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

    def format_only_date_spanish(self, date_str):
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

    def process(self, data):
        """Procesa los mensajes entrantes de Telegram."""
        msg = data.get("message", {})
        chat_id = msg.get("chat", {}).get("id")

        if not chat_id:
            return "ok"

        # Obtener estado actual del usuario
        user_state = USER_STATES.get(chat_id, {"state": "idle"})

        # =====================================================================
        # PASO 1: Usuario saluda y pide cita
        # =====================================================================
        if "text" in msg:
            text = msg["text"]

            # Si el usuario quiere una cita (solo si está idle)
            if self.check_cita_intent(text) and user_state.get("state") == "idle":
                USER_STATES[chat_id] = {"state": "WAITING_INE"}
                self.send_message(chat_id,
                    "¡Hola! 👋 Para agendar tu cita, necesito verificar tu identidad.\n\n"
                    "📷 Por favor, envíame una foto de tu INE (credencial de elector)."
                )
                return "ok"

            # Si el usuario está en proceso de enviar fecha
            if user_state.get("state") == "WAITING_DATE_TIME":
                nombre = user_state.get("nombre", "Cliente")
                fecha_parcial = user_state.get("fecha_parcial")
                hora_parcial = user_state.get("hora_parcial")

                # Parsear la fecha y hora con GPT
                self.send_message(chat_id, "🔍 Procesando...")
                result_json = self.parse_date_time_with_gpt(text)

                try:
                    # Limpiar respuesta si tiene markdown
                    if '```json' in result_json:
                        result_json = result_json.split('```json')[1].split('```')[0]
                    elif '```' in result_json:
                        result_json = result_json.split('```')[1].split('```')[0]

                    parsed = json.loads(result_json.strip())
                    missing = parsed.get("missing")
                    date_str = parsed.get("date")
                    time_str = parsed.get("time")

                    # Manejar caso donde no se entendió nada
                    if missing == "no_entendido" or missing == "error":
                        self.send_message(chat_id,
                            "❌ No pude entender lo que me dices. Por favor intenta de nuevo.\n\n"
                            "Ejemplos:\n"
                            "• 'Mañana a las 3 PM'\n"
                            "• 'En 15 días a las 10 de la mañana'\n"
                            "• 'El próximo sábado a las 7 de la noche'"
                        )
                        return "ok"

                    # Si ya teníamos fecha parcial y ahora viene hora
                    if fecha_parcial and time_str:
                        date_str = fecha_parcial
                    # Si ya teníamos hora parcial y ahora viene fecha
                    if hora_parcial and date_str:
                        time_str = hora_parcial

                    # CASO 1: Faltan ambos
                    if missing == "ambos" or (not date_str and not time_str):
                        self.send_message(chat_id,
                            "❌ Necesito que me digas la fecha y la hora para tu cita.\n\n"
                            "Por ejemplo:\n"
                            "• 'El 25 de febrero a las 3 PM'\n"
                            "• 'En 15 días a las 11 de la mañana'"
                        )
                        return "ok"

                    # CASO 2: Falta hora
                    if missing == "hora" or (date_str and not time_str):
                        # Guardar fecha parcial
                        USER_STATES[chat_id]["fecha_parcial"] = date_str
                        fecha_formateada = self.format_only_date_spanish(date_str)
                        self.send_message(chat_id,
                            f"📅 Entendí la fecha: {fecha_formateada}\n\n"
                            f"⏰ ¿A qué hora te gustaría tu cita?\n\n"
                            f"Nuestro horario es de 10:00 AM a 8:00 PM."
                        )
                        return "ok"

                    # CASO 3: Falta fecha
                    if missing == "fecha" or (time_str and not date_str):
                        # Guardar hora parcial
                        USER_STATES[chat_id]["hora_parcial"] = time_str
                        self.send_message(chat_id,
                            f"⏰ Entendí la hora: {time_str}\n\n"
                            f"📅 ¿Para qué fecha?\n\n"
                            f"Puedes decir:\n"
                            f"• 'Mañana'\n"
                            f"• 'En 15 días'\n"
                            f"• 'El próximo lunes'"
                        )
                        return "ok"

                    # CASO 4: Tenemos fecha y hora - validar
                    is_valid, error_msg, dt = self.validate_date_time(date_str, time_str)

                    if not is_valid:
                        self.send_message(chat_id, error_msg)
                        return "ok"

                    # Crear evento en calendario
                    success, link = self.create_calendar_event(nombre, dt)

                    if success:
                        fecha_formateada = self.format_date_spanish(dt)
                        self.send_message(chat_id,
                            f"🎉 ¡Cita confirmada!\n\n"
                            f"📅 Fecha: {fecha_formateada}\n"
                            f"👤 Nombre: {nombre}\n\n"
                            f"✅ Se ha agendado en nuestro calendario.\n"
                            f"📧 Recibirás recordatorios antes de tu cita.\n\n"
                            f"¡Te esperamos! 😊"
                        )
                    else:
                        self.send_message(chat_id, "❌ Hubo un error al agendar. Intenta de nuevo.")

                    # Limpiar estado
                    USER_STATES.pop(chat_id, None)
                    return "ok"

                except json.JSONDecodeError as e:
                    print(f"❌ Error parseando JSON: {e}")
                    self.send_message(chat_id,
                        "❌ No pude procesar la información. Intenta con otro formato.\n"
                        "Ejemplo: 'Mañana a las 3 PM'"
                    )
                    return "ok"

            # Si no está en ningún flujo especial, mostrar mensaje por defecto
            if user_state.get("state") == "idle":
                self.send_message(chat_id,
                    "Hola 👋 ¿En qué puedo ayudarte?\n\n"
                    "Escribe 'cita' para agendar una cita."
                )
                return "ok"

        # =====================================================================
        # PASO 2: Usuario envía foto del INE
        # =====================================================================
        if "photo" in msg:
            file_id = msg["photo"][-1]["file_id"]

            # Verificar que estamos esperando el INE
            if user_state.get("state") == "WAITING_INE":
                self.send_message(chat_id, "🔍 Verificando tu INE...")

                # Descargar imagen
                img_bytes = self.download_photo(file_id)
                if not img_bytes:
                    self.send_message(chat_id, "❌ No pude descargar la imagen. Intenta de nuevo.")
                    return "ok"

                # Convertir a base64
                img = Image.open(io.BytesIO(img_bytes))
                buff = io.BytesIO()
                img.save(buff, format="JPEG")
                b64 = base64.b64encode(buff.getvalue()).decode()

                # Validar INE con GPT-4o
                result = self.validate_and_extract_ine(b64)

                try:
                    # Limpiar respuesta si tiene markdown
                    result_clean = result
                    if '```json' in result_clean:
                        result_clean = result_clean.split('```json')[1].split('```')[0]
                    elif '```' in result_clean:
                        result_clean = result_clean.split('```')[1].split('```')[0]

                    data = json.loads(result_clean.strip())
                except json.JSONDecodeError as e:
                    print(f"❌ Error parseando INE: {e}")
                    self.send_message(chat_id, "❌ Error al leer el INE. Asegúrate de que la foto sea clara.")
                    return "ok"

                if data.get("validate") is True:
                    nombre = data.get("nombre", "Cliente")
                    USER_STATES[chat_id] = {
                        "state": "WAITING_DATE_TIME",
                        "nombre": nombre
                    }
                    self.send_message(chat_id,
                        f"✅ ¡INE verificado correctamente!\n"
                        f"👤 Nombre: {nombre}\n\n"
                        f"📅 ¿Para qué fecha y hora quieres tu cita?\n\n"
                        f"Puedes escribirlo de forma natural:\n"
                        f"• 'Mañana a las 3 PM'\n"
                        f"• 'En 15 días a las 10 de la mañana'\n"
                        f"• 'El próximo sábado a las 7 de la noche'"
                    )
                else:
                    self.send_message(chat_id,
                        "❌ La imagen no parece ser un INE válido.\n\n"
                        "Por favor, envía una foto clara del frente de tu INE."
                    )
                return "ok"

            # Si envía foto sin estar en flujo de cita, ignorar silenciosamente
            return "ok"

        return "ok"


bot = TelegramBot()

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    return bot.process(request.get_json())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
