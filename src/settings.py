from os.path import abspath
from os.path import dirname
from os.path import join

from pydantic import BaseModel


class Settings(BaseModel):
    date_format = '%d.%m.%Y'
    mongo_uri = (
        '***REDACTED-MONGODB-URI***'
    )
    data_folder = join(dirname(dirname(abspath(__file__))), 'data')


settings = Settings()
