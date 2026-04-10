FROM python:3.10-slim

WORKDIR /code

COPY ./requirements.txt /code/requirements.txt

RUN pip install --no-cache-dir --upgrade -r /code/requirements.txt

COPY ./src /code/src
RUN mkdir /code/data

CMD ["sh", "-c", "sanic src.main:app --host ${WEB_HOST:-0.0.0.0} --port ${WEB_PORT:-8080}"]
