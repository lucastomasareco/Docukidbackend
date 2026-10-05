-- ============================================================
-- Migración: Agregar columna google_email a la tabla users
-- ============================================================
-- Motivo: el notifier necesitaba saber el email de la cuenta
-- Gmail conectada para no tener que llamar a getProfile() en
-- cada vencimiento notificado. Antes el código hacía
-- getattr(user, "google_email", None) que siempre devolvía
-- None porque la columna no existía, resultando en una llamada
-- HTTP desperdiciada por cada correo.
--
-- Ahora: en el callback OAuth (google_oauth.py) llamamos a
-- userinfo.get() y guardamos el email en esta columna. El
-- notifier la usa primero; si es NULL (usuarios antiguos que
-- conectaron Google antes de esta migración) hace fallback al
-- getProfile() como antes.
-- ============================================================

ALTER TABLE users ADD COLUMN google_email TEXT;
