# core/constants.py
from enum import IntEnum

class OrderSide(IntEnum):
    BUY = 0
    SELL = 1

class OrderType(IntEnum):
    MARKET = 1
    LIMIT = 2
    STOP = 3

class BracketType(IntEnum):
    TAKE_PROFIT = 1
    STOP_LOSS = 4

class BarUnit(IntEnum):
    SECOND = 1
    MINUTE = 2
    HOUR = 3
    DAY = 4
    WEEK = 5
    MONTH = 6