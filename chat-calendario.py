from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
creds = service_account.Credentials.from_service_account_file(
 "credentials.json",
 scopes=["https://www.googleapis.com/auth/calendar"]
)
service = build("calendar", "v3", credentials=creds)
event = {
 "summary": "Reserva - Prueba",
 "start": {
 "dateTime": datetime.now().isoformat(),
 "timeZone": "America/Mexico_City"
 },
 "end": {
 "dateTime": (datetime.now() + timedelta(hours=1)).isoformat(),
 "timeZone": "America/Mexico_City"
 }
}
service.events().insert(
 calendarId="ef810304f117b27c6efe3a668038e607f0321921080732f3d5b5a661b9b8a608@group.calendar.google.com"
,
 body=event
).execute()