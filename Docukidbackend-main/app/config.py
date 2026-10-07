"""
config.py
---------
Centraliza la carga y validación de todas las variables de entorno del proyecto
usando Pydantic BaseSettings.

Carga el archivo .env en un solo lugar, valida que todas las variables requeridas
estén presentes (falla rápido al arrancar, listando TODAS las faltantes a la vez),
y expone una instancia `settings` que el resto de los módulos importa en lugar
de usar `os.getenv()` directamente.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        str_strip_whitespace=True,
    )

    # --- Variables REQUIRED (falla rápido si falta alguna o viene vacía) ---
    # STATE_SECRET no tiene fallback a SUPABASE_JWT_SECRET ni a ninguna otra
    # clave: auth.py valida JWT de usuario con JWKS (asimétrico) y el "state"
    # de OAuth se firma con este secreto propio (HS256).
    database_url: str = Field(min_length=1)
    supabase_jwks_url: str = Field(min_length=1)
    state_secret: str = Field(min_length=1)
    cron_secret: str = Field(min_length=1)
    google_client_id: str = Field(min_length=1)
    google_client_secret: str = Field(min_length=1)
    google_redirect_uri: str = Field(min_length=1)
    gemini_api_key: str = Field(min_length=1)

    # --- Variables OPTIONAL (con defaults preservados tal cual estaban) ---
    frontend_url: str = "docukids://"
    env: str = "production"
    gemini_model: str = "gemini-2.0-flash-lite"
    notification_threshold_days: int = 15
    app_timezone: str = "America/Argentina/Buenos_Aires"
    appointment_timezone: str = "America/Argentina/Buenos_Aires"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )


settings = Settings()
settings.env = settings.env.lower()
