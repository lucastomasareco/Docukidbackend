# Docukids - Backend

API de Docukids, una aplicación para que padres, madres y cuidadores gestionen los documentos de sus hijos (DNI, pasaporte, carnet de vacunación, etc.), reciban alertas automáticas de vencimiento y agenden turnos médicos. Todo se respalda en la propia cuenta de Google del usuario, sin costo de infraestructura.

[![Estado](https://img.shields.io/badge/estado-en%20desarrollo-yellow)]()

- Web (frontend): https://docukidsfrontend.vercel.app/
- Repositorio del frontend: https://github.com/lucastomasareco/docukidsfrontend

---

## Tabla de contenidos

- [Descripción](#descripción)
- [Funcionalidades principales](#funcionalidades-principales)
- [Arquitectura](#arquitectura)
- [Modelo de datos](#modelo-de-datos)
- [Endpoints](#endpoints)
- [Stack tecnológico](#stack-tecnológico)
- [Instalación y puesta en marcha](#instalación-y-puesta-en-marcha)
- [Variables de entorno](#variables-de-entorno)
- [Pruebas](#pruebas)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Equipo](#equipo)
- [Tablero y seguimiento](#tablero-y-seguimiento)
- [Licencia](#licencia)

---

## Descripción

Docukids resuelve un problema cotidiano de cuidadores no expertos en tecnología: perder de vista cuándo vence el DNI, el pasaporte u otro documento de un hijo. La app permite sacar una foto o subir un archivo, lee automáticamente la fecha de vencimiento mediante OCR, guarda el archivo en el Google Drive del propio usuario (no en un servidor propio) y envía un recordatorio por correo 15 días antes de cada vencimiento.

Este repositorio contiene el backend. El frontend (web, hecho con Expo) está en un repositorio aparte. El proyecto prioriza un stack **100% gratuito** y una interfaz simple, pensada para usuarios sin conocimientos técnicos.

## Funcionalidades principales

- Validación de sesiones de Supabase Auth (el registro y el login los hace el frontend directamente contra Supabase).
- Conexión con Google (Drive, Calendar, Gmail) mediante OAuth.
- Alta, edición, listado y baja de perfiles de hijos, con foto de perfil.
- Subida de documentos (foto o PDF, hasta 50 MB) con respaldo automático en Google Drive.
- Extracción automática de la fecha de vencimiento por OCR (Gemini Flash / Flash-Lite).
- Estado del documento (vigente, próximo, vencido, sin fecha, sin vencimiento), calculado al vuelo.
- Carpetas de documentos (Documentos, Salud y Escuela de base, más las propias), reflejadas en Drive.
- Papelera de documentos: se pueden restaurar durante 30 días (plazo de la papelera de Drive).
- Recuperación de carpetas de Drive al volver a registrarse tras eliminar la cuenta, y migración de archivos viejos.
- Familias: un titular invita a otras personas con un código; los miembros trabajan sobre los datos del titular.
- Recordatorio automático por correo 15 días antes del vencimiento, sin duplicados.
- Agenda de turnos médicos, sincronizada con Google Calendar.
- Eliminación de cuenta, con la opción de conservar o borrar los archivos de Drive.

## Arquitectura

```
Frontend web (React Native + Expo, TypeScript)  -> Vercel
   │
   ├── Supabase Auth ──── login / registro (JWT)
   │
   └── FastAPI (Render) ── valida el JWT con las claves públicas (JWKS) de Supabase
          │
          ├── PostgreSQL (Supabase) ── metadatos: users, families, children,
          │                             folders, child_folders, documents, appointments
          │
          ├── Google Drive ── almacenamiento de archivos físicos
          ├── Google Calendar ── turnos
          ├── Gmail API ── correos de recordatorio
          └── Gemini API (Google AI Studio) ── OCR de fecha de vencimiento

cron-job.org ── dispara POST /scheduler/check 1 vez por día (cabecera X-Cron-Secret)
                revisa vencimientos y mantiene despierto a Supabase
```

**Principios clave del diseño:**
- El backend **nunca** almacena el contenido de los archivos: solo metadatos y referencias (`drive_file_id`, `drive_link`). Los archivos viven en el Drive del propio usuario.
- El frontend **nunca** maneja tokens de Google; esa responsabilidad es exclusiva del backend.
- El estado de un documento no se guarda: se calcula al vuelo comparando `expiry_date` con la fecha actual.
- Si la persona es miembro de una familia, el backend trabaja con los datos y las credenciales de Google del titular.
- El cron externo cumple doble función: revisa vencimientos y evita que Supabase pause el proyecto por inactividad (plan gratuito).

## Modelo de datos

7 tablas en PostgreSQL (Supabase):

| Tabla | Relación | Descripción |
|---|---|---|
| `users` | — | Usuarios; incluye `google_refresh_token`, `google_email` y `family_id` (vacío = sin familia) |
| `families` | 1:1 con su titular (`owner_id`) | Familia, código de invitación y su vencimiento |
| `children` | N:1 con `users` | Perfiles de hijos (con `avatar_url` y `drive_folder_id`) |
| `folders` | N:1 con `users` | Carpetas de documentos (`is_base` marca las 3 que no se borran) |
| `child_folders` | N:N entre `children` y `folders` | Carpeta de cada categoría dentro de la carpeta de cada hijo en Drive |
| `documents` | N:1 con `children` | Documentos subidos, referencia a Drive; `last_notified_at` evita correos repetidos |
| `appointments` | N:1 con `children` | Turnos médicos, referencia a Calendar |

## Endpoints

La documentación interactiva (Swagger) queda disponible en `/docs` al levantar el servidor. Salvo los indicados, todos los endpoints requieren la cabecera `Authorization: Bearer <token de Supabase>`.

| Grupo | Endpoints |
|---|---|
| General | `GET /` (chequeo liviano, sirve para despertar el servidor) |
| Google | `GET /auth/google/url`, `GET /auth/google/status`, `GET /auth/google/callback` (sin token), `GET /auth/google/url-dev` (solo con `ENV=development`) |
| Cuenta | `DELETE /account` |
| Hijos | `GET /children`, `POST /children`, `PUT /children/{id}`, `DELETE /children/{id}` |
| Documentos | `POST /upload`, `GET /documents/{child_id}`, `PATCH /documents/{id}`, `GET /documents/{id}/thumbnail`, `DELETE /documents/{id}` |
| Carpetas | `GET /folders`, `POST /folders`, `PATCH /folders/{id}`, `DELETE /folders/{id}` |
| Papelera | `GET /trash/documents`, `POST /trash/documents/{drive_file_id}/restore` |
| Recuperación en Drive | `GET /drive/recover/preview`, `POST /drive/recover`, `POST /drive/migrate` |
| Familia | `GET /family`, `POST /family`, `POST /family/invite`, `POST /family/join`, `POST /family/leave`, `DELETE /family/members/{user_id}`, `DELETE /family` |
| Turnos | `GET /appointments/{child_id}`, `POST /appointments`, `PATCH /appointments/{id}`, `DELETE /appointments/{id}` |
| Automático | `POST /scheduler/check` (sin token de usuario: exige la cabecera `X-Cron-Secret`) |

## Stack tecnológico

**Backend**
- FastAPI (Python 3.10+), SQLAlchemy y asyncpg
- Supabase (Auth + PostgreSQL)
- Google APIs: Drive, Calendar, Gmail
- Gemini API (modelos Flash / Flash-Lite; **no usar modelos Pro porque son de pago**)

**Infraestructura**
- Render (hosting del backend, plan gratuito)
- cron-job.org (scheduler externo)

Todo el stack fue elegido para mantenerse dentro de los planes gratuitos de cada servicio.

## Instalación y puesta en marcha

### Requisitos previos
- Python 3.10 o superior
- Un proyecto de Supabase
- Un proyecto de Google Cloud con las APIs de Drive, Calendar y Gmail habilitadas, y un cliente OAuth de tipo **Aplicación web**
- Una API key de Gemini (Google AI Studio)

### Pasos

```bash
git clone https://github.com/lucastomasareco/Docukidbackend.git
cd Docukidbackend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env       # y completar con las credenciales reales
uvicorn app.main:app --reload
```

El servidor queda en `http://localhost:8000` y Swagger en `http://localhost:8000/docs`. Si falta alguna variable obligatoria, el servidor no arranca y lista todas las que faltan.

### Base de datos

El backend no crea las tablas por su cuenta. En el Editor SQL de Supabase hay que crear las tablas base (`users`, `children`, `documents`, `appointments`) y después correr, en este orden, las migraciones de `supabase/migrations`:

1. `add_folders.sql`
2. `add_child_drive_folders.sql`
3. `add_families.sql`
4. `add_users_google_email.sql`
5. `add_documents_no_expiry.sql`
6. `add_documents_mime_type.sql`
7. `add_documents_drive_migrated.sql`

La columna `documents.last_notified_at` (control de correos ya enviados) no tiene migración en este repositorio. Si la base no la tiene, ejecutar:

```sql
ALTER TABLE documents ADD COLUMN last_notified_at DATE;
```

### Despliegue en Render

Cargar las variables de entorno en el panel de Render (nunca en el repositorio). `GOOGLE_REDIRECT_URI` debe ser idéntica, carácter por carácter, a la registrada en Google Cloud y terminar en `/auth/google/callback`. `WEB_ORIGIN` debe ser la dirección de la web, `https://docukidsfrontend.vercel.app`, sin barra final. En cron-job.org se configura una llamada diaria `POST /scheduler/check` con la cabecera `X-Cron-Secret`.

## Variables de entorno

> Ninguna credencial debe subirse al repositorio. Completar `.env` a partir de `env.example`.

**Obligatorias** (el servidor no arranca si falta alguna):

| Variable | Para qué sirve |
|---|---|
| `DATABASE_URL` | Conexión a PostgreSQL en Supabase (`postgresql://...`; el backend la adapta a asyncpg) |
| `SUPABASE_JWKS_URL` | Claves públicas para verificar los tokens: `https://<tu-proyecto>.supabase.co/auth/v1/.well-known/jwks.json` |
| `STATE_SECRET` | Clave propia para firmar el parámetro `state` del OAuth de Google |
| `CRON_SECRET` | Clave que cron-job.org envía en la cabecera `X-Cron-Secret` |
| `GOOGLE_CLIENT_ID` | Cliente OAuth de Google |
| `GOOGLE_CLIENT_SECRET` | Cliente OAuth de Google |
| `GOOGLE_REDIRECT_URI` | Debe coincidir exactamente con la registrada en Google Cloud |
| `GEMINI_API_KEY` | OCR con Gemini |

**Opcionales** (con valor por defecto):

| Variable | Por defecto | Para qué sirve |
|---|---|---|
| `WEB_ORIGIN` | vacío | Dirección de la web; sin ella Google no puede devolver a la versión web, así que en producción hay que cargarla |
| `SUPABASE_SECRET_KEY` | vacío | Permite a `DELETE /account` borrar al usuario de Supabase Auth; sin ella esa ruta devuelve error |
| `FRONTEND_URL` | `docukids://` | Destino de retorno para la app móvil |
| `CORS_ORIGINS` | `*` | Orígenes permitidos, separados por coma; conviene restringirlo a la web |
| `ENV` | `production` | Con el valor `development` se activa `/auth/google/url-dev` |
| `GEMINI_MODEL` | `gemini-2.0-flash-lite` | Modelo de OCR; no usar modelos Pro |
| `NOTIFICATION_THRESHOLD_DAYS` | `15` | Días antes del vencimiento en que se envía el correo |
| `APP_TIMEZONE` | `America/Argentina/Buenos_Aires` | Zona horaria para calcular «hoy» |
| `APPOINTMENT_TIMEZONE` | `America/Argentina/Buenos_Aires` | Zona horaria de los eventos de Google Calendar |

`SUPABASE_JWT_SECRET` ya no se usa: la validación pasó de un secreto compartido a JWKS.

## Pruebas

La carpeta `test/` contiene 117 pruebas automáticas (papelera, recuperación de carpetas, migración a Drive, avatar, baja de cuenta, dueño de los datos). No usan base de datos ni red: Drive y la base se reemplazan por dobles de prueba. Se ejecutan con `pytest`, que no está en `requirements.txt` y hay que instalar aparte (`pip install pytest`).

## Estructura del proyecto

```
Docukidbackend/
├── app/
│   ├── main.py            # App FastAPI y casi todos los endpoints
│   ├── config.py          # Variables de entorno (validadas al arrancar)
│   ├── auth.py            # Validación del JWT de Supabase y dueño de los datos
│   ├── models.py          # Tablas (SQLAlchemy)
│   ├── schemas.py         # Estructuras de entrada y salida (Pydantic)
│   ├── crud.py            # Consultas a la base
│   ├── database.py        # Conexión a PostgreSQL
│   ├── google_oauth.py    # Flujo OAuth de Google
│   ├── google_client.py   # Cliente de las APIs de Google
│   ├── drive_upload.py    # Subida y orden de archivos en Drive
│   ├── drive_trash.py     # Papelera de documentos
│   ├── drive_recover.py   # Recuperación de carpetas de Drive
│   ├── ocr_reader.py      # Lectura de la fecha con Gemini
│   ├── calendar_sync.py   # Eventos de Google Calendar
│   ├── notifier.py        # Correos de recordatorio (Gmail)
│   ├── scheduler.py       # Revisión diaria de vencimientos
│   └── account.py         # Eliminación de cuenta
├── supabase/migrations/   # Migraciones SQL
├── test/                  # Pruebas automáticas
├── env.example            # Plantilla de variables de entorno
└── requirements.txt
```

## Limitaciones conocidas

- **Arranque lento en Render:** en el plan gratuito, el backend puede tardar entre 30 y 60 segundos en responder tras un período de inactividad. El frontend espera hasta 60 segundos y reintenta las lecturas.
- **Gemini en el plan gratuito:** Google puede usar el contenido enviado para mejorar sus modelos. Se acepta esta limitación por tratarse de un proyecto de alcance académico.
- **Sin sincronización con el calendario nativo del dispositivo:** los turnos se crean en Google Calendar.
- **Google en modo prueba:** la pantalla de consentimiento está en modo Externo con usuarios de prueba (hasta 100).
- **Sin analítica de terceros ni rastreo:** no se integra ninguna herramienta de seguimiento, dado que la app maneja datos de menores.

## Equipo

| Integrante | Rol | Responsabilidad principal |
|---|---|---|
| Lucas Areco | Scrum Master \| Full Stack | Metodología ágil, coordinación del equipo, arquitectura frontend/backend |
| Paula Bigorra | Desarrolladora Frontend | Interfaz visual, UX, diseño responsive |
| Patricio Grano | Desarrollador Backend | Lógica de servidor, seguridad, escalabilidad, base de datos |
| Valentina Horociuk | API y QA | Integración con APIs externas (Gmail, Drive), testing y calidad |

## Tablero y seguimiento

- Tablero (Trello): `https://trello.com/b/VW5edIKS`
- Reportes de avance por sprint: `https://drive.google.com/drive/folders/18-OBVJ0SlOMLR772_1hAoEmq8IT1WCLJ?usp=sharing`
- Metodología: Scrum simplificado, backlog priorizado por dependencia técnica.

## Licencia

Proyecto académico. No se definió una licencia.

---

Proyecto desarrollado en el marco de `Práctica Profesional Supervisada | Universidad Nacional Guillermo Brown`.
