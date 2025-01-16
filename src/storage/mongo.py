from datetime import datetime
from typing import Iterable

from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

from src.models.competition import Competition
from src.models.http.student_info import StudentInfo
from src.settings import settings


class MongoAdapter:
    def __init__(self, mongo_uri: str):
        # Create a new client and connect to the server
        client = MongoClient(mongo_uri, server_api=ServerApi('1'))
        client.admin.command('ping')

        self.competitions = client.competitions.competitions

    def get_competitions(self) -> Iterable[Competition]:
        competitions = []

        records = self.competitions.find({})
        for record in records:
            competition = Competition.parse_obj(record)
            competitions.append(competition)

        competitions.sort(key=lambda x: x.created_at)
        return competitions

    def get_filtered(
        self,
        date_from: str,
        date_to: str,
        position: str,
        level: str,
        name: str,
    ) -> list[StudentInfo]:
        filter = {}

        if date_from:
            date_from_dt = datetime.strptime(date_from, settings.date_format)
            filter['date'] = {'$gte': date_from_dt}

        if date_to:
            date_to_dt = datetime.strptime(date_to, settings.date_format)
            if 'date' in filter:
                filter['date'] = filter['date'] | {'$lte': date_to_dt}
            else:
                filter['date'] = {'$lte': date_to_dt}

        if position:
            sign = position[0]
            value = int(position[1])
            if sign == '>':
                filter['position'] = {'$gt': value}
            elif sign == '<':
                filter['position'] = {'$lt': value}

        if level:
            filter['level'] = level

        if name:
            filter['student_name'] = {'$regex': name, '$options': 'i'}

        pipeline = [
            {'$match': filter},
            {
                '$group': {
                    '_id': {
                        'student_id': '$student_id',
                        'student_name': '$student_name',
                        'student_sex': '$student_sex',
                        'institute': '$institute',
                        'group': '$group',
                        'course': '$course',
                    },
                    'count': {'$sum': 1},
                },
            },
        ]
        records = self.competitions.aggregate(pipeline)

        student_infos = []
        for record in records:
            fields = record.get('_id')
            student_info = StudentInfo(
                student_id=fields['student_id'],
                student_name=fields['student_name'],
                student_sex=fields['student_sex'],
                institute=fields['institute'],
                group=fields['group'],
                course=fields['course'],
                count_participation=record['count'],
            )
            student_infos.append(student_info)

        student_infos.sort(key=lambda x: x.count_participation)
        return student_infos

    def save_competitions(self, competitons: Iterable[Competition]):
        records = [item.dict() for item in competitons]
        self.competitions.insert_many(records)

    def clean_db(self):
        self.competitions.delete_many({})
