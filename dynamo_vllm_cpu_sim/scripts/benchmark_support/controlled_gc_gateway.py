"""Opt-in experiment: disable gateway cyclic GC only during measured replay."""
import atexit,gc,json,os,runpy,signal,sys,time,resource
from pathlib import Path
root=Path(os.environ['SIM_STACK_CONFIG']).parents[3];sys.path.insert(0,str(root))
from sim.stack.fine_trace import install_gateway
install_gateway()
import sim.stack.admission_service as service
folder=Path(os.environ['SIM_STACK_CONFIG']).parent
state=dict(initially_enabled=gc.isenabled(),history=[],controls=[])
def callback(phase,info):state['history'].append(dict(t=time.monotonic(),phase=phase,info=dict(info)))
gc.callbacks.append(callback)
def save():
 (folder/'gateway.gc.control.json').write_text(json.dumps(state,separators=(',',':'))+'\n')
original_signal=signal.getsignal(signal.SIGUSR1)
def flush(*args):save();return original_signal(*args)
signal.signal(signal.SIGUSR1,flush)
original_create=service.create_app
def create(*a,**kw):
 app=original_create(*a,**kw)
 @app.post('/diagnostic/gc/disable')
 async def disable():
  if state.get('disabled'):raise RuntimeError('GC experiment already active')
  was_enabled=gc.isenabled();gc.disable();started=time.monotonic();state['disabled']=True
  item=dict(action='disable',t=started,before_enabled=was_enabled,after_enabled=gc.isenabled(),counts=gc.get_count(),max_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
  state['controls'].append(item);save();return item
 @app.post('/diagnostic/gc/restore')
 async def restore():
  if not state.get('disabled'):raise RuntimeError('no active GC experiment')
  started=time.monotonic();before=gc.isenabled()
  if state['initially_enabled']:gc.enable()
  collected=gc.collect();state['disabled']=False
  item=dict(action='restore',t=started,completed_mono=time.monotonic(),before_enabled=before,after_enabled=gc.isenabled(),collected=collected,max_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
  state['controls'].append(item);save();return item
 return app
service.create_app=create
atexit.register(save)
target,*args=sys.argv[1:];sys.argv=[target,*args]
try:runpy.run_path(target,run_name='__main__')
finally:
 if state['initially_enabled']:gc.enable()
 save()
