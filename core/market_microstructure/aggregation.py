"""Streaming non-overlapping event bars and half-open wall-clock buckets."""
from decimal import Decimal as D
from .events import TradeEvent


class EventBars:
    def __init__(self,kind,threshold):
        if kind not in {'TRADE_COUNT','EVENT_COUNT','VOLUME'} or threshold <= 0:
            raise ValueError('invalid event bar')
        self.kind,self.threshold = kind,D(str(threshold))
        self.index = 0
        self.reset()

    def reset(self):
        self.count=0
        self.volume=D(0)
        self.start=self.end=None

    def push(self,event):
        if self.kind in {'TRADE_COUNT','VOLUME'} and not isinstance(event,TradeEvent):
            return ()
        remaining = event.quantity if isinstance(event,TradeEvent) and self.kind=='VOLUME' else D(1)
        result = []
        while remaining:
            self.start = event.event_time if self.start is None else self.start
            self.end = event.event_time
            take = min(remaining,self.threshold-(self.volume if self.kind=='VOLUME' else D(self.count)))
            if self.kind=='VOLUME':
                self.volume += take
            else:
                self.count += int(take)
            remaining -= take
            if (self.volume if self.kind=='VOLUME' else D(self.count)) == self.threshold:
                result.append({'index':self.index,'start':self.start,'end':self.end,
                               'count':self.count,'volume':self.volume,'kind':self.kind})
                self.index += 1
                self.reset()
        return tuple(result)


class ClockBuckets:
    def __init__(self,width_ms):
        if width_ms not in {100,250,500,1000}:
            raise ValueError('unsupported bucket width')
        self.width=width_ms
        self.start=None
        self.count=0

    def push(self,event):
        start = event.event_time//self.width*self.width
        if self.start is not None and start < self.start:
            raise ValueError('out-of-order bucket event')
        result=[]
        if self.start is not None and start!=self.start:
            for boundary in range(self.start,start,self.width):
                result.append({'start':boundary,'end_exclusive':boundary+self.width,
                               'event_count':self.count if boundary==self.start else 0})
            self.count=0
        self.start=start
        self.count+=1
        return tuple(result)
