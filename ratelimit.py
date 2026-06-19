"""Rate limiter compartido (slowapi), por IP de origen."""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
