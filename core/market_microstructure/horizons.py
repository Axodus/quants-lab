"""Windows are (t-h,t]; price anchors are the latest valid state at/before t-h."""
from enum import Enum
from collections import deque
from .models import HORIZONS


class Horizon(Enum):
    H_100MS = 100
    H_250MS = 250
    H_500MS = 500
    H_1S = 1000
    H_2S = 2000
    H_5S = 5000
    H_10S = 10000

    @classmethod
    def all_ms(cls):
        return list(HORIZONS)


class BoundedWindow:
    def __init__(self, duration, capacity):
        self.duration, self.capacity = duration, capacity
        self.items = deque()

    def append(self, time, value):
        if self.items and time < self.items[-1][0]:
            raise ValueError('out-of-order availability')
        self.prune(time)
        if len(self.items) >= self.capacity:
            raise ValueError('WINDOW_CAPACITY_EXCEEDED: no silent data loss')
        self.items.append((time, value))

    def prune(self, time):
        while len(self.items) > 1 and self.items[1][0] <= time-self.duration:
            self.items.popleft()

    def since(self, time, duration):
        return [value for t,value in self.items if time-duration < t <= time]

    def anchor(self, time):
        found = None
        for t, value in self.items:
            if t > time:
                break
            found = value
        return found
