"""旧审计入口兼容当前 EDF；旧 slack 日志使用其冻结审计器。"""
from check_edf_priority import check

if __name__=='__main__':
    import argparse,json
    from pathlib import Path
    p=argparse.ArgumentParser();p.add_argument('folder',type=Path);a=p.parse_args()
    result=check(a.folder)
    (a.folder/'edf_priority.check.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='samples'}))
