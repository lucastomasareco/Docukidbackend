-- ============================================================
-- Migración: Lugar del turno (v2.7)
-- ============================================================
-- Motivo: cada turno puede indicar DÓNDE es (por ejemplo, el
-- consultorio del pediatra). Se guarda el nombre del lugar, la
-- dirección escrita y las coordenadas para mostrar el mapa.
--
-- Todas las columnas son opcionales: los turnos que ya existen
-- quedan sin lugar (NULL) y siguen funcionando igual.
-- IF NOT EXISTS permite correrla más de una vez sin error.
--
-- La tabla appointments ya tiene RLS activo; no se toca.
--
-- ⚠️ CORRER ESTE SQL EN SUPABASE ANTES DE SUBIR EL CÓDIGO NUEVO A RENDER.
-- ============================================================

ALTER TABLE appointments
  ADD COLUMN IF NOT EXISTS location_name TEXT,
  ADD COLUMN IF NOT EXISTS address      TEXT,
  ADD COLUMN IF NOT EXISTS latitude     DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS longitude    DOUBLE PRECISION;

-- Red de seguridad: las coordenadas van juntas (las dos o ninguna) y dentro
-- de rango. El backend ya lo valida; esto protege la base si alguien escribe
-- directo. El bloque DO evita error si la restricción ya existe.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'appointments_coords_validas'
  ) THEN
    ALTER TABLE appointments
      ADD CONSTRAINT appointments_coords_validas CHECK (
        (latitude IS NULL AND longitude IS NULL)
        OR (
          latitude  BETWEEN -90  AND 90
          AND longitude BETWEEN -180 AND 180
        )
      );
  END IF;
END $$;

-- ============================================================
-- COMPROBACIONES (correr después, una por una)
-- ============================================================
-- 1) Las 4 columnas existen (debe devolver 4 filas):
--    SELECT column_name, data_type FROM information_schema.columns
--    WHERE table_name = 'appointments'
--      AND column_name IN ('location_name','address','latitude','longitude');
--
-- 2) Los turnos viejos se leen y tienen el lugar vacío:
--    SELECT id, title, location_name, latitude FROM appointments LIMIT 5;
--
-- 3) La restricción rechaza una latitud sin longitud (debe dar error):
--    UPDATE appointments SET latitude = 10 WHERE id = (SELECT min(id) FROM appointments);
--    (Si da error "violates check constraint", está bien. No quedó nada guardado.)
