"""
notifier.py
Fase 2, paso 5 de la Guía Técnica: envía el correo "tu documento vence en
N días" usando la API de Gmail. Es, según la propia Guía Técnica, "la
función más importante del proyecto" -- así que este archivo prioriza que
un error acá sea RUIDOSO (se propaga como excepción) en vez de fallar en
silencio. Quien llama a enviar_aviso_vencimiento (scheduler.py, en el
siguiente paso) es responsable de decidir qué hacer si falla un envío
puntual (típicamente: no marcar el documento como notificado, para
reintentar al día siguiente).
--- Decisión: Gmail API en vez de smtplib (la Guía ofrece las dos opciones) ---
Elegí la API de Gmail porque ya le pedimos el scope "gmail.send" al usuario
en google_oauth.py (Fase 2, paso 1), así que no hace falta pedirle además
una "contraseña de aplicación" de Gmail por separado: reutilizamos el mismo
refresh_token que ya usan Drive y Calendar.
--- Sobre a quién le llega el correo ---
Según Docukids.md, sección 2-B, el aviso llega "al correo Gmail asociado al
usuario". Como actuamos EN NOMBRE del usuario (con su propio refresh_token),
el correo se manda desde su cuenta de Gmail hacia esa misma cuenta (un
"correo a uno mismo", como un recordatorio). No hace falta configurar
ningún remitente propio del proyecto.
"""
import base64
from datetime import date
from email.mime.text import MIMEText
from fastapi import HTTPException
from googleapiclient.discovery import build
from .google_client import build_google_credentials
from .models import User


def _armar_asunto_y_cuerpo(nombre_hijo: str, nombre_documento: str, fecha_vencimiento: date, dias_restantes: int) -> tuple[str, str]:
    if dias_restantes < 0:
        asunto = f"🔴 {nombre_documento} de {nombre_hijo} está VENCIDO"
        situacion = f"venció el {fecha_vencimiento.strftime('%d/%m/%Y')} (hace {abs(dias_restantes)} días)"
    elif dias_restantes == 0:
        asunto = f"⚠️ {nombre_documento} de {nombre_hijo} vence HOY"
        situacion = "vence hoy"
    else:
        asunto = f"⚠️ {nombre_documento} de {nombre_hijo} vence en {dias_restantes} días"
        situacion = f"vence el {fecha_vencimiento.strftime('%d/%m/%Y')} (en {dias_restantes} días)"
    
    cuerpo = (
        f"Hola!\n\n"
        f"Te avisamos que el documento \"{nombre_documento}\" de {nombre_hijo} {situacion}.\n\n"
        f"Revisá la documentación a tiempo para evitar sorpresas de último momento.\n\n"
        f"— Docukids\n"
    )
    return asunto, cuerpo


def _construir_mensaje_raw(destinatario: str, asunto: str, cuerpo_texto: str) -> dict:
    """
    Arma el mensaje en el formato que pide la API de Gmail: un MIME
    codificado en base64 "url-safe" (con - y _ en vez de + y /, sin
    padding visible), dentro de la clave "raw".
    """
    mensaje = MIMEText(cuerpo_texto, _charset="utf-8")
    mensaje["to"] = destinatario
    mensaje["subject"] = asunto
    raw = base64.urlsafe_b64encode(mensaje.as_bytes()).decode("utf-8")
    return {"raw": raw}


def enviar_aviso_vencimiento(
    user: User,
    nombre_hijo: str,
    nombre_documento: str,
    fecha_vencimiento: date,
    dias_restantes: int,
) -> None:
    """
    Envía el correo de aviso. NO atrapa excepciones: si falla (token
    revocado, rate limit de Gmail, lo que sea), la excepción sube hasta
    quien llamó esta función. Es intencional: scheduler.py necesita saber
    si el envío falló para NO marcar el documento como "ya notificado" y
    así reintentar al día siguiente.
    """
    credenciales = build_google_credentials(user, "Gmail")
    servicio = build("gmail", "v1", credentials=credenciales)

    # Preferimos el email que guardamos en users.google_email durante el
    # callback OAuth (ahorra una llamada HTTP extra en cada vencimiento).
    # Puede ser None en un caso legítimo: usuarios que conectaron Google
    # ANTES de que se agregara la columna y este backfill no se haya
    # corrido sobre sus filas existentes. En ese caso hacemos fallback a
    # getProfile().
    email_destino = user.google_email

    if not email_destino:
        perfil = servicio.users().getProfile(userId="me").execute()
        email_destino = perfil.get("emailAddress")

    if not email_destino:
        raise HTTPException(
            status_code=500,
            detail="No se pudo determinar el email de la cuenta de Google autorizada"
        )

    asunto, cuerpo = _armar_asunto_y_cuerpo(nombre_hijo, nombre_documento, fecha_vencimiento, dias_restantes)
    
    # Usamos el email de Google como destino, garantizando el recordatorio "a uno mismo"
    mensaje = _construir_mensaje_raw(email_destino, asunto, cuerpo)
    servicio.users().messages().send(userId="me", body=mensaje).execute()