"""
ocr_reader.py
Fase 2, paso 3 de la Guía Técnica: usa Gemini (modelo Flash/Flash-Lite, API
Key de Google AI Studio) para leer un documento y detectar su fecha de
vencimiento.
Usa el SDK actual google-genai (paquete `google-genai`, se importa como
`from google import genai`). El paquete `google-generativeai` que se había
usado en la primera versión de este archivo está OFICIALMENTE DESCONTINUADO
(el propio import tira un FutureWarning pidiendo migrar), así que se
reemplazó por indicación explícita para no quedar con una dependencia muerta.
Cómo lo dividimos (siguiendo la Guía Técnica al pie de la letra, sección 1:
"Le pasamos la imagen y ella nos devuelve el texto; NOSOTROS buscaremos la
fecha de vencimiento"):
Gemini SOLO transcribe el texto que ve en la imagen/PDF, tal cual está
escrito. No le pedimos que "interprete" ni que nos devuelva JSON con la
fecha ya decidida.
El backend (funciones _extraer_fecha_de_texto / extraer_fecha_vencimiento
de acá abajo) es el que busca, dentro de ese texto, una línea que hable
de vencimiento y le saca la fecha con expresiones regulares.
¿Por qué separarlo así en vez de pedirle todo a Gemini de una? Porque así,
si Gemini transcribe bien pero se "confunde" interpretando cuál es la fecha
de vencimiento (por ejemplo, la confunde con la fecha de nacimiento), es
más fácil de debuggear: podemos ver el texto crudo que devolvió e ir
ajustando las palabras clave/regex, en vez de pelearnos con un prompt.
⚠️ Nota de privacidad (ya la marca la Guía Técnica, sección 1): en el nivel
gratuito de Gemini, Google puede usar el contenido enviado para mejorar sus
modelos. Estamos mandando documentos de menores (DNI, carnets de vacunación).
Esto queda documentado como limitación aceptada por alcance académico.
"""
import re
from datetime import date
from typing import Optional
from google import genai
from google.genai import types
from .config import settings

GEMINI_API_KEY = settings.gemini_api_key

# gemini-2.0-flash-lite / gemini-flash: los únicos gratuitos verificados en
# sept. 2026 (Guía Técnica, sección 1). NUNCA cambiar a un modelo "Pro" acá:
# dejó de ser gratuito y la llamada fallaría o cobraría sin avisar.
GEMINI_MODEL = settings.gemini_model

# El cliente se crea una sola vez (no hay costo en crearlo antes de tener la
# API key a mano; recién falla si de verdad intentás llamar a generate_content
# sin key configurada).
_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if not GEMINI_API_KEY:
        raise RuntimeError("Falta GEMINI_API_KEY en el .env")
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ---------- Paso 1: transcripción con Gemini ----------

_PROMPT_TRANSCRIPCION = (
    "Transcribí TODO el texto visible en esta imagen o documento, tal cual "
    "está escrito, línea por línea. No traduzcas, no corrijas errores, no "
    "interpretes ni resumas nada, no agregues comentarios tuyos. Si hay "
    "tablas o campos de un formulario, transcribilos renglón por renglón "
    "como aparecen. Si la imagen no tiene texto legible, respondé "
    "exactamente y solamente: SIN_TEXTO_LEGIBLE"
)


def _transcribir_documento(file_bytes: bytes, mime_type: str) -> str:
    cliente = _get_client()
    respuesta = cliente.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            _PROMPT_TRANSCRIPCION,
        ],
    )
    return (respuesta.text or "").strip()


# ---------- Paso 2: el backend busca la fecha de vencimiento en ese texto ----------

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Palabras que indican que la fecha de esa línea es una fecha de VENCIMIENTO
# (y no, por ejemplo, la fecha de nacimiento o de emisión del documento).
_PALABRAS_CLAVE_VENCIMIENTO = (
    "vencimiento", "vence", "vencido", "caduca", "caducidad",
    "válido hasta", "valido hasta", "valid until", "expira", "expiry",
    "fecha de expiración", "fecha de expiracion",
)

_RE_FECHA_TEXTUAL = re.compile(
    r"(\d{1,2})\s+de\s+(" + "|".join(MESES.keys()) + r")(?:\s+de\s+(\d{4}))?",
    re.IGNORECASE,
)

# 🔧 BUG ARREGLADO: el guion "-" ahora va al principio de los corchetes [-/.]
# para que no se interprete como un rango inválido entre "/" y "."
# (antes estaba [/.-] que en Python 3.13 tira PatternError: bad character range)
_RE_FECHA_NUMERICA = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2,4}))?\b")


def _completar_anio(dia: int, mes: int, anio_texto: Optional[str]) -> Optional[int]:
    """
    Si el documento trae año, lo usamos directo (completando "26" -> 2026).
    Si NO trae año (ej. la cartilla que en Docukids.md dice "Vence: 10/10"),
    asumimos el año actual, salvo que esa fecha ya haya pasado hace más de
    medio año: en ese caso asumimos que es del año que viene (evita marcar
    como "vencido hace 11 meses" algo que en realidad vence el mes que viene).
    """
    if anio_texto is not None:
        anio = int(anio_texto)
        return anio + 2000 if anio < 100 else anio

    hoy = date.today()
    try:
        candidata = date(hoy.year, mes, dia)
    except ValueError:
        return None

    if (hoy - candidata).days > 183:
        return hoy.year + 1
    return hoy.year


def _extraer_fecha_de_fragmento(fragmento: str) -> Optional[date]:
    match = _RE_FECHA_TEXTUAL.search(fragmento)
    if match:
        dia = int(match.group(1))
        mes = MESES[match.group(2).lower()]
        anio = _completar_anio(dia, mes, match.group(3))
        if anio is None:
            return None
        try:
            return date(anio, mes, dia)
        except ValueError:
            return None

    match = _RE_FECHA_NUMERICA.search(fragmento)
    if match:
        dia, mes = int(match.group(1)), int(match.group(2))
        anio = _completar_anio(dia, mes, match.group(3))
        if anio is None:
            return None
        try:
            return date(anio, mes, dia)
        except ValueError:
            return None

    return None


def extraer_fecha_vencimiento(texto_ocr: str) -> Optional[date]:
    """
    Recorre el texto transcripto línea por línea buscando alguna que mencione
    vencimiento, y le saca la fecha. A propósito NO devuelve "la primera
    fecha que encuentre en todo el documento" si no hay ninguna palabra
    clave: podría ser la fecha de nacimiento, de emisión, etc. En ese caso
    devuelve None y el documento queda con status "sin_fecha" para revisar
    a mano (ver compute_status en crud.py).
    """
    lineas = texto_ocr.splitlines()
    for i, linea in enumerate(lineas):
        if any(palabra in linea.lower() for palabra in _PALABRAS_CLAVE_VENCIMIENTO):
            fecha = _extraer_fecha_de_fragmento(linea)
            if fecha:
                return fecha
            # A veces, en formularios de dos columnas, la etiqueta y el
            # valor quedan en líneas separadas ("VENCIMIENTO" / "10/10/2026").
            if i + 1 < len(lineas):
                fecha = _extraer_fecha_de_fragmento(lineas[i + 1])
                if fecha:
                    return fecha
    return None


# ---------- Punto de entrada que usa main.py ----------

def leer_fecha_de_vencimiento(file_bytes: bytes, mime_type: str) -> Optional[date]:
    """
    Esta función NUNCA lanza una excepción hacia afuera: si Gemini falla
    (rate limit, timeout, modelo caído, API key sin configurar) o no
    encuentra fecha, devuelve None. La Guía Técnica recomienda explícitamente
    tener este fallback ("fecha manual mientras pruebas") para que /upload
    nunca se rompa por un problema del lado de Gemini.
    """
    try:
        texto = _transcribir_documento(file_bytes, mime_type)
    except Exception as error:
        # 🔧 DEBUG TEMPORAL: imprimimos el error real de Gemini para ver
        # por qué estaba devolviendo None en silencio. Sacar estos prints
        # cuando el problema esté resuelto.
        print(f"ERROR EN GEMINI: {error}")
        return None

    # 🔧 DEBUG TEMPORAL: imprimimos el texto crudo que devolvió Gemini,
    # así podemos ver si el regex no matchea por un tema de formato.
    print(f"TEXTO TRANSCRITO POR GEMINI: {texto}")

    if not texto or texto.strip().upper() == "SIN_TEXTO_LEGIBLE":
        return None

    return extraer_fecha_vencimiento(texto)