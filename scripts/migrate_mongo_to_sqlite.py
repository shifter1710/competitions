import argparse
from datetime import datetime

from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

from src.models.competition import Competition
from src.storage.sqlite import SQLiteAdapter


def parse_args():
    parser = argparse.ArgumentParser(description='Migrate competitions data from MongoDB to SQLite.')
    parser.add_argument('--mongo-uri', required=True)
    parser.add_argument('--mongo-db', default='competitions')
    parser.add_argument('--mongo-collection', default='competitions')
    parser.add_argument('--sqlite-path', required=True)
    parser.add_argument('--drop-sqlite-data', action='store_true')
    return parser.parse_args()


def main():
    args = parse_args()

    client = MongoClient(args.mongo_uri, server_api=ServerApi('1'))
    client.admin.command('ping')
    collection = client[args.mongo_db][args.mongo_collection]

    sqlite = SQLiteAdapter(args.sqlite_path)
    if args.drop_sqlite_data:
        sqlite.clean_db()

    records = []
    for record in collection.find({}).sort('created_at', 1):
        created_at = record.get('created_at') or datetime.utcnow()
        competition = Competition.parse_obj(
            {
                '_id': str(record['_id']),
                'Код студента': record['student_id'],
                'ФИО': record['student_name'],
                'Пол': record['student_sex'],
                'Институт': record['institute'],
                'Группа': record['group'],
                'Курс': record['course'],
                'Вид спорта': record['sport'],
                'Дата': record['date'],
                'Уровень соревнований': record['level'],
                'Название соревнований': record['name'],
                'Место': record['position'],
                'Время создания записи (UTC)': created_at,
            }
        )
        records.append(competition)

    sqlite.save_competitions(records)
    print(f'migrated {len(records)} records to {args.sqlite_path}')


if __name__ == '__main__':
    main()
