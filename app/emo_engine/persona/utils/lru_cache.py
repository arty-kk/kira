#app/emo_engine/persona/utils/lru_cache.py
from collections import OrderedDict

class LRUCache(OrderedDict):

    def __init__(self, *args, maxsize: int = 1024, **kwargs):
        self._maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self:
            del self[key]
        elif len(self) >= self._maxsize:
            self.popitem(last=False)
        super().__setitem__(key, value)