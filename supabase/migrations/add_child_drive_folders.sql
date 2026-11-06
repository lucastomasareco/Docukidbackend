-- ============================================================
-- Migración: Drive por hijo (v2.6) - una carpeta de Drive por cada niño
-- ============================================================
-- Motivo (pedido de la profesora): hoy los archivos de todos los hijos se
-- mezclan en las mismas carpetas de Drive. Desde la v2.6 cada hijo tiene su
-- carpeta "Docukids / {IDhijo}_{Nombre}" y, dentro, sus propias subcarpetas
-- (Salud, Escuela, ...). Ver Guía Técnica, sección 13.
--
-- Qué agrega:
--   1) children.drive_folder_id : id de la carpeta del hijo en Drive.
--                                 NULL = todavía no se creó (se crea al primer uso).
--   2) tabla child_folders      : para cada par (hijo, carpeta de la app), el id de
--                                 su subcarpeta en Drive.
--
-- Es segura de correr más de una vez (IF NOT EXISTS) y NO toca datos
-- existentes: todos los hijos quedan con drive_folder_id = NULL y
-- child_folders arranca vacía. No cambia nada de lo que hace el backend
-- actual (el backend viejo ignora estas columnas), así que se puede correr
-- antes de desplegar sin romper nada.
--
-- folders.drive_folder_id queda SIN USO a partir de la v2.6 (no se borra).
--
-- ORDEN: primero esta migración en Supabase, DESPUÉS el deploy a Render.
-- ============================================================

-- 1) Carpeta del hijo en Drive. NULL = todavía no creada.
ALTER TABLE children
    ADD COLUMN IF NOT EXISTS drive_folder_id TEXT;

-- 2) Subcarpeta (Salud, Escuela, ...) de cada hijo en Drive.
--    Si se borra el hijo o la carpeta de la app, la fila se borra sola (CASCADE).
CREATE TABLE IF NOT EXISTS child_folders (
    child_id INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    drive_folder_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (child_id, folder_id)
);

-- La clave primaria ya cubre las búsquedas por hijo; este índice acelera las
-- búsquedas por carpeta (renombrar o borrar una carpeta de la app).
CREATE INDEX IF NOT EXISTS child_folders_folder_id_idx
    ON child_folders (folder_id);

-- Mismo criterio de seguridad que las otras tablas: RLS activo (7 tablas).
ALTER TABLE child_folders ENABLE ROW LEVEL SECURITY;

-- ============================================================
-- COMPROBACIÓN (correr después, en el Editor SQL; son solo lecturas)
-- ============================================================
-- a) La columna nueva existe (debe devolver 1 fila: drive_folder_id | text):
--    SELECT column_name, data_type FROM information_schema.columns
--     WHERE table_name = 'children' AND column_name = 'drive_folder_id';
--
-- b) La tabla existe y está vacía (debe devolver 0):
--    SELECT count(*) FROM child_folders;
--
-- c) RLS activo en la tabla nueva (debe devolver: child_folders | true):
--    SELECT relname, relrowsecurity FROM pg_class WHERE relname = 'child_folders';
--
-- d) Los hijos de siempre no cambiaron (drive_folder_id debe ser NULL en todos):
--    SELECT id, name, drive_folder_id FROM children;
