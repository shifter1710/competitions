from os.path import abspath
from os.path import dirname
from os.path import join

from pydantic import Field
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


BASE_DIR = dirname(dirname(abspath(__file__)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    date_format: str = '%d.%m.%Y'
    data_folder: str = Field(default=join(BASE_DIR, 'data'), alias='DATA_FOLDER')
    database_path: str = Field(default=join(BASE_DIR, 'data', 'competitions.sqlite3'), alias='DATABASE_PATH')
    auth_cookie_name: str = Field(default='competitions_auth', alias='AUTH_COOKIE_NAME')
    auth_secret_key: str = Field(default='change-me', alias='AUTH_SECRET_KEY')
    auth_admin_username: str = Field(default='admin', alias='AUTH_ADMIN_USERNAME')
    auth_admin_password: str = Field(default='change-me', alias='AUTH_ADMIN_PASSWORD')
    auth_editor_username: str = Field(default='editor', alias='AUTH_EDITOR_USERNAME')
    auth_editor_password: str = Field(default='change-me-editor', alias='AUTH_EDITOR_PASSWORD')
    auth_viewer_username: str = Field(default='', alias='AUTH_VIEWER_USERNAME')
    auth_viewer_password: str = Field(default='', alias='AUTH_VIEWER_PASSWORD')
    auth_session_ttl_seconds: int = Field(default=43200, alias='AUTH_SESSION_TTL_SECONDS')


settings = Settings()
