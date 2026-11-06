-- ============================================================
-- Migración: Drive por hijo (v2.6, paso 6) - marca de documentos migrados
-- ============================================================
-- Motivo: POST /drive/migrate mueve y renombra los documentos que se subieron
-- ANTES de la v2.6 a su carpeta de hijo ("Docukids / {IDhijo}_{Nombre} / ..."),
-- de a lotes de 10. Para saber cuáles faltan hace falta una marca por documento.
-- Ver Guía Técnica, sección 13.
--
-- Qué agrega:
--   documents.drive_migrated : true = el archivo ya está en la carpeta de su hijo
--                              y con su nombre nuevo (o no existe más en Drive).
--                              false = todavía hay que migrarlo.
--
-- Es segura de correr más de una vez (IF NOT EXISTS) y NO cambia datos: todos los
-- documentos de siempre quedan en false. El backend viejo ignora la columna, así
-- que se puede correr antes de desplegar sin romper nada.
--
-- ORDEN: primero esta migración en Supabase, DESPUÉS el deploy a Render.
-- ============================================================

ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS drive_migrated BOOLEAN NOT NULL DEFAULT false;

-- ============================================================
-- COMPROBACIÓN (correr después, en el Editor SQL; son solo lecturas)
-- ============================================================
-- a) La columna existe (debe devolver 1 fila: drive_migrated | boolean | NO):
--    SELECT column_name, data_type, is_nullable FROM information_schema.columns
--     WHERE table_name = 'documents' AND column_name = 'drive_migrated';
--
-- b) Todos los documentos de siempre están pendientes (false) y ninguno es NULL:
--    SELECT drive_migrated, count(*) FROM documents GROUP BY drive_migrated;
