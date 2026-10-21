"""Fixed public work rules; base capacity is not the buff-adjusted game limit."""
import hashlib
import json
from pathlib import Path

from .errors import ApiError

DEFAULT_MANAGEMENT_CONFIG = Path(__file__).resolve().parents[3] / 'data/home/management.json'
MANAGEMENT_SHA256 = 'c9bc70b5d9afb0a88f24a7e4f8afbd831cc92cd1d41b55d9d778594bf79fedd7'
# Hot64 LumiStateMutex[action=1]: states 2,3,5,6,7. Work1/collect4 are not mutex.
WORK_MUTEX_MASK = 236


class HomeWorkConfig:
    def __init__(self, path=DEFAULT_MANAGEMENT_CONFIG):
        self.path = Path(path)
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                raw = self.path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != MANAGEMENT_SHA256:
                    raise ValueError()
                document = json.loads(raw)
                self._data = ({row['building_id']: row for row in document['buildings']},
                              {row['recipe_id']: row for row in document['recipes']},
                              {row['pet_id']: row for row in document['pets']})
            except (OSError, ValueError, KeyError, TypeError):
                raise ApiError('HOME_CONFIG_UNAVAILABLE', '家园工作配置暂不可用', 503) from None
        return self._data

    def building(self, building_id):
        return self._load()[0].get(str(building_id))

    def recipe(self, recipe_id):
        return self._load()[1].get(str(recipe_id))

    def work_types(self, pet_id):
        pet = self._load()[2].get(str(pet_id))
        return {row['work_type_id'] for row in pet['work_abilities']} if pet is not None else None

    def level(self, building_id, level):
        building = self.building(building_id)
        return next((row for row in building['levels'] if row['level'] == level), None) if building else None
