-- ============================================================
-- Migración: documentos vinculados a un turno
-- ============================================================
-- Motivo: al agendar un turno (por ejemplo el pediatra) se pueden
-- vincular documentos que ya existen en Docs del niño (por ejemplo
-- "Análisis de sangre"). Un turno puede tener varios documentos y un
-- documento puede estar en varios turnos.
--
-- Se guarda en una tabla aparte (appointment_documents), una fila por
-- vínculo. Las dos claves son ON DELETE CASCADE:
--   - si se borra el DOCUMENTO, el vínculo desaparece solo (el turno queda);
--   - si se borra el TURNO, sus vínculos desaparecen (el documento queda).
--
-- Es segura de correr más de una vez (IF NOT EXISTS) y no toca datos
-- existentes: los turnos actuales quedan sin documentos.
--
-- ⚠️ CORRER ESTE SQL EN SUPABASE ANTES DE SUBIR EL CÓDIGO NUEVO A RENDER.
-- ============================================================

CREATE TABLE IF NOT EXISTS appointment_documents (
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    document_id    INTEGER NOT NULL REFERENCES documents(id)    ON DELETE CASCADE,
    created_at     TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (appointment_id, document_id)
);

-- Para buscar rápido "¿en qué turnos está este documento?".
CREATE INDEX IF NOT EXISTS appointment_documents_document_idx
    ON appointment_documents (document_id);

-- Mismo criterio de seguridad que las otras tablas: RLS activo.
ALTER TABLE appointment_documents ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- COMPROBACIONES (correr después, una por una)
-- ============================================================
-- 1) La tabla existe y está vacía (debe devolver 0):
--    SELECT count(*) FROM appointment_documents;
--
-- 2) Borrar un documento desvincula (probar con datos de prueba):
--    vincular un documento a un turno desde la app, borrarlo desde Docs y
--    ver que el turno sigue y el vínculo ya no está:
--    SELECT * FROM appointment_documents WHERE document_id = <id borrado>;  -- 0 filas
-- ============================================================
