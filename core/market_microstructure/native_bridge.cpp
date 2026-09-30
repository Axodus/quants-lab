// Accessors around the EXISTING event-atomic kernel. No second book algorithm.
#include "../../research_notebooks/orderflow_backtest/orderbook_kernel.cpp"
extern "C" {
I mm_apply(void* p,const I* rows,I n){auto k=(Kernel*)p;k->push(rows,n);if(!k->error){k->finish_event();k->pending=false;}return k->error;}
I mm_top(void* p,I side,I depth,I* out){auto k=(Kernel*)p;I n=0;if(side){for(auto [price,qty]:k->asks){if(n==depth)break;out[2*n]=price;out[2*n+1]=qty;++n;}}else{for(auto i=k->bids.rbegin();i!=k->bids.rend()&&n<depth;++i){out[2*n]=i->first;out[2*n+1]=i->second;++n;}}return n;}
I mm_qty(void* p,I side,I price){auto k=(Kernel*)p;auto& b=side?k->asks:k->bids;auto i=b.find(price);return i==b.end()?0:i->second;}
I mm_time(void* p){return ((Kernel*)p)->lasttime;}
I mm_sequence(void* p){return ((Kernel*)p)->last;}
}
