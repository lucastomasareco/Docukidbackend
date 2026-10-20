-- ============================================================
-- Migración: Agregar columna mime_type a la tabla documents
-- ============================================================
-- Motivo (v2.4, miniaturas y visor de imágenes): la app necesita
-- saber si un documento es una imagen o un PDF para decidir si
-- lo abre en el visor propio o en Drive.
--
-- Es segura de correr sobre datos existentes: los documentos
-- actuales quedan con mime_type = NULL ("todavía no se sabe").
-- El backend lo completa solo la primera vez que se pide la
-- miniatura de un documento viejo.
-- IF NOT EXISTS permite correrla más de una vez sin error.
--
-- ⚠️ CORRER ESTE SQL ANTES DE SUBIR EL CÓDIGO NUEVO A RENDER.
-- ============================================================

ALTER TABLE documents
  ADD COLUMN IF NOT EXISTS mime_type TEXT;
