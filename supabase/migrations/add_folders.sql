-- ============================================================
-- Migración: carpetas de documentos (tabla folders + documents.folder_id)
-- ============================================================
-- Motivo: las carpetas (Salud, Escuela, etc.) dejan de ser solo del
-- teléfono y pasan a existir en la base y en Google Drive.
--
-- Es segura de correr más de una vez (IF NOT EXISTS) y no toca los
-- datos existentes: todos los documentos actuales quedan con
-- folder_id = NULL (= sueltos, en la raíz "Docukids" de Drive).
--
-- ORDEN: primero esta migración en Supabase, DESPUÉS el deploy a Render.
-- ============================================================

CREATE TABLE IF NOT EXISTS folders (
    id SERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    is_base BOOLEAN NOT NULL DEFAULT false,   -- Documentos, Salud, Escuela: se renombran, no se borran
    drive_folder_id TEXT,                     -- NULL = todavía no creada en Drive (se crea al primer uso)
    created_at TIMESTAMP DEFAULT NOW()
);

-- Un usuario no puede tener dos carpetas con el mismo nombre (sin mirar mayúsculas).
CREATE UNIQUE INDEX IF NOT EXISTS folders_user_name_unique
    ON folders (user_id, lower(name));

-- NULL = documento suelto ("A la vista"), en la raíz "Docukids".
-- Si se borra una carpeta de la base, sus documentos quedan sueltos (no se borran).
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL;

-- Mismo criterio de seguridad que las otras tablas: RLS activo.
ALTER TABLE folders ENABLE ROW LEVEL SECURITY;
