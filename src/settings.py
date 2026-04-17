from os.path import abspath
from os.path import dirname
from os.path import join

from pydantic import BaseSettings
from pydantic import Field


BASE_DIR = dirname(dirname(abspath(__file__)))


class Settings(BaseSettings):
    date_format: str = '%d.%m.%Y'
    data_folder: str = Field(default=join(BASE_DIR, 'data'), env='DATA_FOLDER')
    database_path: str = Field(default=join(BASE_DIR, 'data', 'competitions.sqlite3'), env='DATABASE_PATH')
    auth_cookie_name: str = Field(default='competitions_auth', env='AUTH_COOKIE_NAME')
    auth_secret_key: str = Field(default='change-me', env='AUTH_SECRET_KEY')
    auth_admin_username: str = Field(default='admin', env='AUTH_ADMIN_USERNAME')
    auth_admin_password: str = Field(default='change-me', env='AUTH_ADMIN_PASSWORD')
    auth_viewer_username: str = Field(default='', env='AUTH_VIEWER_USERNAME')
    auth_viewer_password: str = Field(default='', env='AUTH_VIEWER_PASSWORD')

    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'


settings = Settings()
