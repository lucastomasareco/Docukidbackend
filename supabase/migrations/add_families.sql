-- Familias (v2.5): dos cuentas (titular y un miembro) que ven los mismos datos.
-- Segura de repetir. Correr en el Editor SQL de Supabase ANTES de desplegar a Render.

CREATE TABLE IF NOT EXISTS families (
    id SERIAL PRIMARY KEY,
    owner_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,   -- el titular
    invite_code TEXT UNIQUE,              -- NULL = sin invitación activa
    invite_expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS family_id INTEGER REFERENCES families(id) ON DELETE SET NULL;  -- NULL = sin familia

ALTER TABLE families ENABLE ROW LEVEL SECURITY;
