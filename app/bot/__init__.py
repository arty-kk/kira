#app/bot/__init__.py
def start_bot(*args, **kwargs):
    from .components.webhook import start_bot as _start_bot
    return _start_bot(*args, **kwargs)

__all__ = ["start_bot"]