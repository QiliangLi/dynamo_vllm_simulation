"""旧模块名兼容入口：当前实现统一为 EDF；历史 slack 须使用冻结源码。"""
from sim.stack.edf_priority import (PREFIX,FIELDS,enabled,validate,attach_http,
    from_annotations,calculate,metadata,refresh,emit,install)
