from os.path import abspath
from os.path import dirname
from os.path import join

from pydantic import BaseSettings
from pydantic import Field


BASE_DIR = dirname(dirname(abspath(__file__)))


class Settings(BaseSettings):
    date_format: str = '%d.%m.%Y'
    mongo_uri: str = Field(default='mongodb://127.0.0.1:27017', env='MONGO_URI')
    mongo_db_name: str = Field(default='competitions', env='MONGO_DB_NAME')
    mongo_collection_name: str = Field(default='competitions', env='MONGO_COLLECTION_NAME')
    data_folder: str = Field(default=join(BASE_DIR, 'data'), env='DATA_FOLDER')
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
