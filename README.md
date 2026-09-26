# Docukids

Aplicación móvil para que padres, madres y cuidadores gestionen los documentos de sus hijos (DNI, pasaporte, carnet de vacunación, etc.), reciban alertas automáticas de vencimiento y agenden turnos médicos — todo respaldado en la propia cuenta de Google del usuario, sin costo de infraestructura.

[![Estado](https://img.shields.io/badge/estado-en%20desarrollo-yellow)]()
[![Licencia](https://img.shields.io/badge/licencia-%5B...%5D-blue)]()

---

## Tabla de contenidos

- [Descripción](#descripción)
- [Funcionalidades principales](#funcionalidades-principales)
- [Arquitectura](#arquitectura)
- [Modelo de datos](#modelo-de-datos)
- [Stack tecnológico](#stack-tecnológico)
- [Instalación y puesta en marcha](#instalación-y-puesta-en-marcha)
- [Variables de entorno](#variables-de-entorno)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Equipo](#equipo)
- [Tablero y seguimiento](#tablero-y-seguimiento)
- [Licencia](#licencia)

---

## Descripción

Docukids resuelve un problema cotidiano de cuidadores no expertos en tecnología: perder de vista cuándo vence el DNI, el pasaporte u otro documento de un hijo. La app permite sacar una foto o subir un PDF, lee automáticamente la fecha de vencimiento mediante OCR, guarda el archivo en el Google Drive del propio usuario (no en un servidor propio) y envía un recordatorio por correo 15 días antes de cada vencimiento.

El proyecto prioriza un stack **100% gratuito** y una interfaz simple, pensada para usuarios sin conocimientos técnicos.

## Funcionalidades principales

- Registro e inicio de sesión (Supabase Auth).
- Autorización con Google (Drive, Calendar, Gmail) mediante OAuth.
- Alta, listado y baja de perfiles de hijos.
- Subida de documentos (foto o PDF) con respaldo automático en Google Drive.
- Extracción automática de la fecha de vencimiento por OCR (Gemini Flash / Flash-Lite).
- Estado visual del documento (vigente / por vencer / vencido), calculado al vuelo.
- Recordatorio automático por correo 15 días antes del vencimiento (idempotente).
- Agenda de turnos médicos, sincronizada con Google Calendar.
- Búsqueda de documentos por etiquetas.
- Botón principal de guardado grande, pensado para cuidadores no expertos.

## Arquitectura

```
App móvil (React Native + Expo, TypeScript)
   │
   ├── Supabase Auth ──── login / registro (JWT)
   │
   └── FastAPI (Render) ── valida JWT contra Supabase
          │
          ├── PostgreSQL (Supabase) ── metadatos: users, children,
          │                             documents, appointments, alert_log
          │
          ├── Google Drive ── almacenamiento de archivos físicos
          ├── Google Calendar ── turnos
          ├── Gmail API ── correos de recordatorio
          └── Gemini API (Google AI Studio) ── OCR de fecha de vencimiento

cron-job.org ── dispara GET /scheduler/check 1x/día (X-Cron-Secret)
                revisa vencimientos y mantiene despierto a Supabase
```

**Principios clave del diseño:**
- El backend **nunca** almacena el contenido de los archivos — solo metadatos y referencias (`drive_file_id`, `drive_link`). Los archivos viven en el Drive del propio usuario.
- El frontend **nunca** manipula tokens de Google; esa responsabilidad es exclusiva del backend.
- El estado de un documento (vigente/por vencer/vencido) no se persiste: se calcula al vuelo comparando `expiry_date` con la fecha actual.
- El cron externo cumple doble función: revisa vencimientos y evita que Supabase pause el proyecto por inactividad (free tier).

Diagrama completo: `[link al diagrama en el documento de entrega / draw.io / dbdiagram.io]`

## Modelo de datos

5 tablas en PostgreSQL (Supabase):

| Tabla | Relación | Descripción |
|---|---|---|
| `users` | — | Usuarios registrados, incluye `google_refresh_token` |
| `children` | N:1 con `users` | Perfiles de hijos |
| `documents` | N:1 con `children` | Documentos subidos, referencia a Drive |
| `appointments` | N:1 con `children` | Turnos médicos, referencia a Calendar |
| `alert_log` | N:1 con `documents` | Control de idempotencia de correos enviados |

Detalle completo de campos: ver documento de entrega, sección "Modelo de datos".

## Stack tecnológico

**Frontend**
- React Native + Expo Router (TypeScript)
- `@supabase/supabase-js`
- Axios (con interceptor JWT)
- `expo-image-picker`
- `react-native-calendars`

**Backend**
- FastAPI (Python 3.10+)
- Supabase (Auth + PostgreSQL)
- Google APIs: Drive, Calendar, Gmail
- Gemini API (modelos Flash / Flash-Lite — **Pro prohibido por costo**)

**Infraestructura**
- Render (hosting del backend, free/Hobby)
- cron-job.org (scheduler externo)

Todo el stack fue elegido para mantenerse dentro de los planes gratuitos de cada servicio.

## Instalación y puesta en marcha

### Requisitos previos
- Node.js `[versión]`
- Python 3.10+
- Cuenta de Supabase y proyecto creado
- Cuenta de Google Cloud con OAuth configurado (Drive, Calendar, Gmail APIs habilitadas)
- Cuenta de Google AI Studio (API key de Gemini)
- `expo-cli` / `eas-cli` `[versión]`

### Backend

```bash
cd [carpeta-backend]
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # completar con las credenciales reales
uvicorn app.main:app --reload
```

### Frontend

```bash
cd [carpeta-frontend]
npm install
cp .env.example .env      # completar con las credenciales reales
npx expo start
```

### Generar APK de release (Android)

```bash
expo run:android --variant release
```

## Variables de entorno

> Ninguna credencial debe subirse al repositorio. Completar `.env` a partir de `.env.example`.

**Backend (`.env`)**
```
SUPABASE_URL=
SUPABASE_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=
GEMINI_API_KEY=
X_CRON_SECRET=
[completar con el resto de las variables reales del proyecto]
```

**Frontend (`.env`)**
```
EXPO_PUBLIC_SUPABASE_URL=
EXPO_PUBLIC_SUPABASE_ANON_KEY=
EXPO_PUBLIC_API_URL=
[completar con el resto de las variables reales del proyecto]
```

## Estructura del proyecto

```
docukids/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── auth/
│   │   ├── scheduler.py
│   │   ├── notifier.py
│   │   ├── ocr_reader.py
│   │   ├── drive_upload.py
│   │   ├── calendar_sync.py
│   │   └── google_oauth.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── app/            # Expo Router
│   ├── components/
│   ├── package.json
│   └── .env.example
└── README.md
```

## Limitaciones conocidas

- **Cold start de Render**: al estar en el plan gratuito, el backend puede tardar entre 30 y 60 segundos en responder tras un período de inactividad. La app muestra feedback de carga durante este lapso.
- **Gemini free tier**: en el nivel gratuito, Google puede usar el contenido enviado para mejorar sus modelos. Se acepta esta limitación por tratarse de un proyecto de alcance académico.
- **Sin sincronización con el calendario nativo del dispositivo**: los turnos se crean en Google Calendar, no en el calendario nativo de iOS/Android (fuera del alcance del MVP).
- **Sin analítica de terceros ni rastreo**: no se integra ninguna herramienta de tracking, dado que la app maneja datos de menores.
- **Android únicamente**: no hay build para iOS ni versión web en el alcance actual.

## Equipo

| Integrante | Rol | Responsabilidad principal |
|---|---|---|
| Lucas Areco | Scrum Master \| Full Stack | Metodología ágil, coordinación del equipo, arquitectura frontend/backend |
| Paula Bigorra | Desarrolladora Frontend | Interfaz visual, UX, diseño responsive |
| Patricio Grano | Desarrollador Backend | Lógica de servidor, seguridad, escalabilidad, base de datos |
| Valentina Horociuk | API y QA | Integración con APIs externas (Gmail, Drive), testing y calidad |

## Tablero y seguimiento

- Tablero (Trello): `[link real al tablero]`
- Reportes de avance por sprint: `[link a la carpeta o documento de reportes]`
- Metodología: Scrum simplificado, backlog priorizado por dependencia técnica.

## Licencia

`[definir licencia del proyecto, ej. MIT, o "Proyecto académico — uso no comercial"]`

---

Proyecto desarrollado en el marco de `[nombre de la cátedra / materia / institución]`.
