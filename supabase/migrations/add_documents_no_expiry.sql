-- ============================================================
-- Migración: Agregar columna no_expiry a la tabla documents
-- ============================================================
-- Motivo: tras el OCR, el usuario puede confirmar, corregir o
-- indicar que el documento "no vence" (cartilla, radiografías).
-- Sin esta columna, "no vence" sería indistinguible de "el OCR
-- no encontró fecha" (ambos tendrían expiry_date NULL).
--
-- Es segura de correr sobre datos existentes: todos los
-- documentos actuales quedan con no_expiry = false.
-- IF NOT EXISTS permite correrla más de una vez sin error.
-- ============================================================

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS no_expiry BOOLEAN NOT NULL DEFAULT false;
