// Internal fixed-point mutation kernel. Event atomicity survives all batch/file boundaries.
#include <array>
#include <map>
#include <vector>
#include <cstdint>
#include <algorithm>
using I=int64_t;
struct Frame { I t,bid,ask,bd,ad,events,seq; };
struct Kernel {
 std::map<I,I> bids,asks;
 std::array<I,7> key{};
 bool pending=false,live=false,bridge=false;
 I last=-1,lasttime=-1,start,end,minute,depth,events=0,rows=0,snapshots=0,crosses=0;
 I error=0; // 1 sequence, 2 chronology, 3 level, 4 OOS, 5 empty, 6 cross
 std::array<I,16> evidence{};
 std::vector<Frame> frames;
 Kernel(I s,I e,I d):start(s),end(e),minute(s),depth(d){}
 void fail(I code){if(!error){error=code;evidence[0]=code;for(int j=0;j<7;j++)evidence[1+j]=key[j];evidence[8]=last;evidence[9]=bids.empty()?0:bids.rbegin()->first;evidence[10]=asks.empty()?0:asks.begin()->first;evidence[11]=rows;evidence[12]=events;}}
 void emit(){if(minute>end)return;if(bids.empty()||asks.empty()){fail(5);return;} I bd=0,ad=0,n=0;for(auto i=bids.rbegin();i!=bids.rend()&&n++<depth;++i)bd+=i->second;n=0;for(auto i=asks.begin();i!=asks.end()&&n++<depth;++i)ad+=i->second; frames.push_back({minute+59999,bids.rbegin()->first,asks.begin()->first,bd,ad,events,last});minute+=60000;}
 void finish_event(){if(!pending||!live)return; ++events; if(bids.empty()||asks.empty()){fail(5);return;}if(bids.rbegin()->first>=asks.begin()->first){++crosses;fail(6);}last=(key[2]?key[6]:key[4]);lasttime=key[0];}
 void push(const I* a,I n){for(I i=0;i<n&&!error;++i){const I* r=a+10*i;std::array<I,7> k;std::copy(r,r+7,k.begin());if(!pending||k!=key){finish_event();if(error)return;pending=false;if(r[0]>end){key=k;fail(4);return;}
 // Ignore pre-snapshot increments; bootstrap is supplied explicitly in the input stream.
 if(!live&&!r[2])continue;
 if(live&&r[0]<lasttime && !(bridge && !r[2] && r[3]<=last && last<=r[4] && r[0]/60000==lasttime/60000)){key=k;fail(2);return;}
 while(live&&minute<r[0]-r[0]%60000&&minute<=end){emit();if(error)return;}
 key=k;pending=true;
 if(r[2]){bids.clear();asks.clear();live=true;bridge=true;++snapshots;}
 else {if(bridge){if(!(r[3]<=last && last<=r[4]) && r[5]!=last){fail(1);return;}bridge=false;}else if(r[5]!=last){fail(1);return;}}
 }
 if(r[8]<=0||r[9]<0||r[7]<0||r[7]>1){fail(3);return;}auto& b=r[7]?asks:bids;if(r[9]==0)b.erase(r[8]);else b[r[8]]=r[9];++rows;
 }}
};
extern "C" {
void* ob_new(I s,I e,I d){return new Kernel(s,e,d);}
void ob_free(void* p){delete (Kernel*)p;}
I ob_push(void* p,const I* a,I n){auto k=(Kernel*)p;k->push(a,n);return k->error;}
I ob_finish(void* p,I flush){auto k=(Kernel*)p;k->finish_event();k->pending=false;if(flush&&!k->error)while(k->minute<=k->end){k->emit();if(k->error)break;}return k->error;}
I ob_frames(void* p,I* out){auto k=(Kernel*)p;I n=k->frames.size();if(out)std::copy((I*)k->frames.data(),(I*)k->frames.data()+n*7,out);return n;}
void ob_info(void* p,I* out){auto k=(Kernel*)p;std::copy(k->evidence.begin(),k->evidence.end(),out);out[13]=k->events;out[14]=k->snapshots;out[15]=k->rows;}
I ob_levels(void* p,I side,I* out){auto k=(Kernel*)p;auto& b=side?k->asks:k->bids;I n=0;for(auto [price,qty]:b){if(out){out[n*2]=price;out[n*2+1]=qty;}++n;}return n;}
}
