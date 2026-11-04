// Counted per-block disk model. Called under the existing Python service lock.
// No SimGrid, worker threads, networking, or change to admission semantics.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <limits>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr double inf=std::numeric_limits<double>::infinity();
struct Group {uint64_t id;int disk;uint64_t count;double remaining,ready,demand,rate=0;};
struct Store {
  int disks;double capacity,now=0,total=0,total_correction=0;
  std::vector<Group> groups;
  std::vector<bool> valid;
  std::vector<double> next_ready,bytes,correction,peaks;
  std::map<uint64_t,int> pending;
  uint64_t rate_rebuilds=0,waterfill_calls=0;
  Store(int d,double c):disks(d),capacity(c),valid(d,false),next_ready(d,inf),bytes(d,0),correction(d,0),peaks(d,0){}
  void rates(){
    for(int d=0;d<disks;++d){
      if(valid[d] && next_ready[d]>now+1e-12)continue;
      ++rate_rebuilds;next_ready[d]=inf;
      std::vector<size_t> items;double count=0,demand=0;
      for(size_t i=0;i<groups.size();++i){auto& g=groups[i];if(g.disk!=d)continue;
        if(g.ready<=now+1e-12){items.push_back(i);count+=g.count;demand+=g.demand*g.count;}
        else{g.rate=0;next_ready[d]=std::min(next_ready[d],g.ready);}}
      if(!items.empty()){
        ++waterfill_calls;
        if(demand<=capacity){double bonus=(capacity-demand)/count;
          for(auto i:items)groups[i].rate=groups[i].demand+bonus;
        }else{
          std::stable_sort(items.begin(),items.end(),[&](size_t i,size_t j){return groups[i].demand<groups[j].demand;});
          double remaining=capacity;
          for(auto i:items){auto& g=groups[i];g.rate=std::min(g.demand,remaining/count);
            remaining-=g.rate*g.count;count-=g.count;}
        }
      }
      valid[d]=true;
    }
  }
  double boundary(){
    double t=inf;bool positive=true;
    for(const auto& g:groups){
      if(g.rate>0){t=std::min(t,now+g.remaining/g.rate);if(g.remaining<=0)positive=false;}
      if(g.ready>now+1e-12)t=std::min(t,g.ready);}
    if(t==now && positive)t=std::nextafter(now,inf);
    return t;
  }
  double next(){rates();return boundary();}
  void submit(double t,uint64_t id,const uint64_t* counts,double block_bytes,double demand,double latency){
    if(t!=now || pending.count(id) || !std::isfinite(block_bytes) || !std::isfinite(demand) || !std::isfinite(latency)
       || block_bytes<=0 || demand<=0 || latency<0)throw std::runtime_error("invalid counted submission");
    int n=0;for(int d=0;d<disks;++d)if(counts[d])++n;
    if(!n)throw std::runtime_error("empty counted submission");
    // Reserve before mutating to avoid a partially submitted request on allocation failure.
    groups.reserve(groups.size()+n);pending.emplace(id,n);
    for(int d=0;d<disks;++d)if(counts[d]){
      groups.push_back({id,d,counts[d],block_bytes,now+latency,demand,0});valid[d]=false;}
  }
  int advance(double target,uint64_t* done,size_t capacity_done){
    if(capacity_done<pending.size())throw std::runtime_error("completion buffer too small");
    rates();
    if(!std::isfinite(target) || target<now-1e-12 || target>boundary()+1e-9)throw std::runtime_error("invalid advance boundary");
    std::vector<double> flux(disks,0);
    for(auto& g:groups)if(g.rate>0){
      flux[g.disk]+=g.rate*g.count;
      double per=std::min(g.remaining,g.rate*(target-now));g.remaining-=per;
      double amount=per*g.count;
      double value=amount-correction[g.disk],updated=bytes[g.disk]+value;
      correction[g.disk]=(updated-bytes[g.disk])-value;bytes[g.disk]=updated;
      value=amount-total_correction;updated=total+value;total_correction=(updated-total)-value;total=updated;
    }
    for(int d=0;d<disks;++d)peaks[d]=std::max(peaks[d],flux[d]);
    now=target;int count=0;
    for(const auto& g:groups)if(g.remaining<=0){
      auto it=pending.find(g.id);
      if(--it->second==0){done[count++]=g.id;pending.erase(it);}
      valid[g.disk]=false;
    }
    groups.erase(std::remove_if(groups.begin(),groups.end(),[](const Group& g){return g.remaining<=0;}),groups.end());
    return count;
  }
  std::string snapshot(){
    rates();std::ostringstream out;out<<std::setprecision(17);
    out<<"{\"now\":"<<now<<",\"bytes\":"<<total<<",\"rate_rebuilds\":"<<rate_rebuilds
       <<",\"waterfill_calls\":"<<waterfill_calls<<",\"disk_bytes\":[";
    for(int d=0;d<disks;++d)out<<(d?",":"")<<bytes[d];
    out<<"],\"peaks\":[";for(int d=0;d<disks;++d)out<<(d?",":"")<<peaks[d];
    out<<"],\"groups\":[";bool first=true;
    for(const auto& g:groups){out<<(first?"":",")<<'['<<g.id<<','<<g.disk<<','<<g.count<<','<<g.remaining<<','<<g.ready<<','<<g.demand<<','<<g.rate<<']';first=false;}
    out<<"]}";return out.str();
  }
};
thread_local std::string error,output;
}
extern "C" {
const char* gs_error(){return error.c_str();}
void* gs_create(int disks,double capacity){try{
  if(disks<=0 || !std::isfinite(capacity) || capacity<=0)throw std::runtime_error("invalid disk configuration");
  return new Store(disks,capacity);
}catch(const std::exception& e){error=e.what();return nullptr;}}
void gs_destroy(void* p){delete static_cast<Store*>(p);}
int gs_submit(void* p,double now,uint64_t id,const uint64_t* counts,double bytes,double demand,double latency){try{
  static_cast<Store*>(p)->submit(now,id,counts,bytes,demand,latency);return 0;
}catch(const std::exception& e){error=e.what();return -1;}}
int gs_next(void* p,double* out){try{*out=static_cast<Store*>(p)->next();return 0;}catch(const std::exception& e){error=e.what();return -1;}}
int gs_advance(void* p,double target,uint64_t* out,size_t capacity){try{
  return static_cast<Store*>(p)->advance(target,out,capacity);
}catch(const std::exception& e){error=e.what();return -1;}}
const char* gs_snapshot(void* p){try{output=static_cast<Store*>(p)->snapshot();return output.c_str();}catch(const std::exception& e){error=e.what();return nullptr;}}
double gs_bytes(void* p){return static_cast<Store*>(p)->total;}
}
