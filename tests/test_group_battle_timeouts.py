import importlib.util
import pathlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def hset(self, key, mapping=None, **kwargs):
        self.ops.append(("hset", key, mapping or kwargs))

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))

    def delete(self, key):
        self.ops.append(("delete", key))

    def hincrby(self, key, field, value):
        self.ops.append(("hincrby", key, field, value))

    async def execute(self):
        for op in self.ops:
            if op[0] == "hset":
                _, key, mapping = op
                cur = self.redis.hashes.setdefault(key, {})
                for k, v in mapping.items():
                    cur[str(k)] = str(v)
            elif op[0] == "expire":
                continue
            elif op[0] == "delete":
                _, key = op
                self.redis.kv.pop(key, None)
                self.redis.hashes.pop(key, None)
            elif op[0] == "hincrby":
                _, key, field, value = op
                cur = self.redis.hashes.setdefault(key, {})
                cur[field] = str(int(cur.get(field, "0")) + int(value))
        self.ops.clear()


class FakeLock:
    async def acquire(self):
        return True

    async def release(self):
        return None


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.hashes = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.kv:
            return False
        self.kv[key] = str(value)
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, key):
        self.kv.pop(key, None)
        self.hashes.pop(key, None)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, field=None, value=None, mapping=None):
        cur = self.hashes.setdefault(key, {})
        if mapping is not None:
            for k, v in mapping.items():
                cur[str(k)] = str(v)
        else:
            cur[str(field)] = str(value)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hincrby(self, key, field, value):
        cur = self.hashes.setdefault(key, {})
        cur[field] = str(int(cur.get(field, "0")) + int(value))
        return int(cur[field])

    async def expire(self, key, ttl):
        return True

    async def exists(self, key):
        return key in self.kv or key in self.hashes

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def lock(self, _name, timeout=5, blocking_timeout=0):
        return FakeLock()

    async def smembers(self, _key):
        return set()

    async def zrangebyscore(self, _key, _min, _max):
        return []


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []

    async def get_chat_member(self, _chat_id, uid):
        user = SimpleNamespace(id=uid, username=f"u{uid}", full_name=f"User {uid}")
        return SimpleNamespace(user=user)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=101)

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class _FakeTelegramBadRequest(Exception):
    pass


def _load_group_battle_module(bot_id=777):
    module_name = "group_battle_under_test"
    target_modules = {
        "app": types.ModuleType("app"),
        "app.clients": types.ModuleType("app.clients"),
        "app.clients.telegram_client": types.ModuleType("app.clients.telegram_client"),
        "app.config": types.ModuleType("app.config"),
        "app.core": types.ModuleType("app.core"),
        "app.core.memory": types.ModuleType("app.core.memory"),
        "app.bot": types.ModuleType("app.bot"),
        "app.bot.components": types.ModuleType("app.bot.components"),
        "app.bot.components.constants": types.ModuleType("app.bot.components.constants"),
        "app.bot.utils": types.ModuleType("app.bot.utils"),
        "app.bot.utils.debouncer": types.ModuleType("app.bot.utils.debouncer"),
        "app.tasks": types.ModuleType("app.tasks"),
        "app.tasks.battle": types.ModuleType("app.tasks.battle"),
        "aiogram": types.ModuleType("aiogram"),
        "aiogram.types": types.ModuleType("aiogram.types"),
        "aiogram.exceptions": types.ModuleType("aiogram.exceptions"),
        "redis": types.ModuleType("redis"),
        "redis.exceptions": types.ModuleType("redis.exceptions"),
    }

    target_modules["app.clients.telegram_client"].get_bot = lambda: FakeBot()
    target_modules["app.config"].settings = SimpleNamespace(ALLOWED_GROUP_IDS=[999])
    target_modules["app.core.memory"].get_redis = lambda: None
    target_modules["app.core.memory"]._b2s = lambda x: x.decode() if isinstance(x, bytes) else ("" if x is None else str(x))
    target_modules["app.bot.components.constants"].BOT_ID = bot_id
    target_modules["app.bot.utils.debouncer"].buffer_message_for_response = lambda _payload: None

    class _InlineKeyboardButton:
        def __init__(self, text, callback_data):
            self.text = text
            self.callback_data = callback_data

    class _InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    target_modules["aiogram.types"].InlineKeyboardButton = _InlineKeyboardButton
    target_modules["aiogram.types"].InlineKeyboardMarkup = _InlineKeyboardMarkup
    target_modules["aiogram.types"].CallbackQuery = object
    target_modules["aiogram.exceptions"].TelegramBadRequest = _FakeTelegramBadRequest
    target_modules["redis.exceptions"].LockError = Exception

    target_modules["app.tasks.battle"].battle_start_timeout_check_task = SimpleNamespace(apply_async=Mock())
    target_modules["app.tasks.battle"].battle_move_timeout_check_task = SimpleNamespace(apply_async=Mock())

    previous = {}
    names = set(target_modules) | {module_name}
    for name in names:
        previous[name] = sys.modules.get(name)
        sys.modules.pop(name, None)

    try:
        sys.modules.update(target_modules)
        path = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "addons" / "group_battle.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if previous[name] is not None:
                sys.modules[name] = previous[name]


class GroupBattleTimeoutTests(unittest.IsolatedAsyncioTestCase):

    async def test_launch_battle_uses_runtime_bot_id_after_import(self):
        mod = _load_group_battle_module(bot_id=None)
        redis = FakeRedis()
        bot = FakeBot()
        mod.get_redis = lambda: redis
        mod.bot = bot

        mod.consts.BOT_ID = 4242

        start_task = SimpleNamespace(apply_async=Mock())
        prev_battle_module = sys.modules.get("app.tasks.battle")
        sys.modules["app.tasks.battle"] = types.SimpleNamespace(
            battle_start_timeout_check_task=start_task,
            battle_move_timeout_check_task=SimpleNamespace(apply_async=Mock()),
        )
        try:
            await mod.launch_battle("4242", "7", chat_id=999)
        finally:
            if prev_battle_module is None:
                sys.modules.pop("app.tasks.battle", None)
            else:
                sys.modules["app.tasks.battle"] = prev_battle_module

        ready_keys = [k for k in redis.kv if k.startswith("ready:")]
        self.assertEqual(len(ready_keys), 1)
        self.assertTrue(ready_keys[0].endswith(":4242"))

    async def test_launch_battle_schedules_start_timeout_via_celery(self):
        mod = _load_group_battle_module()
        redis = FakeRedis()
        bot = FakeBot()
        mod.get_redis = lambda: redis
        mod.bot = bot

        start_task = SimpleNamespace(apply_async=Mock())
        prev_battle_module = sys.modules.get("app.tasks.battle")
        sys.modules["app.tasks.battle"] = types.SimpleNamespace(
            battle_start_timeout_check_task=start_task,
            battle_move_timeout_check_task=SimpleNamespace(apply_async=Mock()),
        )
        try:
            await mod.launch_battle("1", "2", chat_id=999)
        finally:
            if prev_battle_module is None:
                sys.modules.pop("app.tasks.battle", None)
            else:
                sys.modules["app.tasks.battle"] = prev_battle_module

        start_task.apply_async.assert_called_once()
        kwargs = start_task.apply_async.call_args.kwargs
        self.assertEqual(kwargs["countdown"], int(mod.T_START.total_seconds()))
        payload = kwargs["kwargs"]["payload"]
        self.assertIn("gid", payload)
        self.assertEqual(payload["expected_phase_version"], 1)

    async def test_on_battle_start_schedules_move_timeout_via_celery(self):
        mod = _load_group_battle_module()
        redis = FakeRedis()
        bot = FakeBot()
        mod.get_redis = lambda: redis
        mod.bot = bot

        gid = "g-start"
        redis.hashes[f"game:{gid}"] = {
            "state": "CREATED",
            "version": "1",
            "phase_version": "1",
            "chat_id": "999",
            "player1_id": "1",
            "player2_id": "2",
            "player1_name": "P1",
            "player2_name": "P2",
            "msg_id": "55",
            "ts": "2024-01-01T00:00:00+00:00",
            "choice1": "",
            "choice2": "",
        }
        redis.kv[f"ready:{gid}:2"] = "1"

        move_task = SimpleNamespace(apply_async=Mock())
        prev_battle_module = sys.modules.get("app.tasks.battle")
        sys.modules["app.tasks.battle"] = types.SimpleNamespace(
            battle_start_timeout_check_task=SimpleNamespace(apply_async=Mock()),
            battle_move_timeout_check_task=move_task,
        )

        query = SimpleNamespace(
            data=f"battle_start:{gid}",
            from_user=SimpleNamespace(id=1, username="u1", full_name="U1"),
            answer=lambda cache_time=0: None,
            message=SimpleNamespace(reply_markup=None),
        )

        async def _answer(cache_time=0):
            return None

        query.answer = _answer

        try:
            await mod.on_battle_start(query)
        finally:
            if prev_battle_module is None:
                sys.modules.pop("app.tasks.battle", None)
            else:
                sys.modules["app.tasks.battle"] = prev_battle_module

        move_task.apply_async.assert_called_once()
        kwargs = move_task.apply_async.call_args.kwargs
        self.assertEqual(kwargs["countdown"], int((mod.T_MOVE + mod.SAFETY).total_seconds()))
        payload = kwargs["kwargs"]["payload"]
        self.assertEqual(payload["gid"], gid)
        self.assertEqual(payload["expected_phase_version"], 2)

    async def test_timeout_guard_skips_cancel_when_version_changed(self):
        mod = _load_group_battle_module()
        redis = FakeRedis()
        bot = FakeBot()
        mod.get_redis = lambda: redis
        mod.bot = bot

        gid = "g-guard"
        redis.hashes[f"game:{gid}"] = {
            "state": "CREATED",
            "version": "3",
            "phase_version": "3",
            "chat_id": "999",
            "player1_id": "1",
            "player2_id": "2",
        }

        await mod.check_battle_timeout(gid, expected_phase_version=2)

        self.assertEqual(bot.sent, [])
        self.assertIn(f"game:{gid}", redis.hashes)

    async def test_timeout_task_is_idempotent(self):
        mod = _load_group_battle_module()
        redis = FakeRedis()
        bot = FakeBot()
        mod.get_redis = lambda: redis
        mod.bot = bot

        gid = "g-idempotent"
        redis.hashes[f"game:{gid}"] = {
            "state": "CREATED",
            "version": "1",
            "phase_version": "1",
            "chat_id": "999",
            "player1_id": "1",
            "player2_id": "2",
        }
        redis.kv["active_game:999"] = gid

        await mod.check_battle_timeout(gid, expected_phase_version=1)
        await mod.check_battle_timeout(gid, expected_phase_version=1)

        self.assertEqual(bot.sent, [])
        self.assertNotIn(f"game:{gid}", redis.hashes)
        self.assertNotIn("active_game:999", redis.kv)

    async def test_timeout_deletes_created_battle_message_without_cancel_notice(self):
        mod = _load_group_battle_module()
        redis = FakeRedis()
        bot = FakeBot()
        mod.get_redis = lambda: redis
        mod.bot = bot

        gid = "g-delete-msg"
        redis.hashes[f"game:{gid}"] = {
            "state": "CREATED",
            "version": "1",
            "phase_version": "1",
            "chat_id": "999",
            "player1_id": "1",
            "player2_id": "2",
            "msg_id": "55",
        }
        redis.kv["active_game:999"] = gid

        await mod.check_battle_timeout(gid, expected_phase_version=1)

        self.assertEqual(bot.deleted, [(999, 55)])
        self.assertEqual(bot.sent, [])


if __name__ == "__main__":
    unittest.main()
