"""Check representation changes against the standard JSON decoder."""
import json, tempfile, unittest
from pathlib import Path
from sim.trace_io import load_trace


class TraceTests(unittest.TestCase):
    def test_values_types_and_non_aliasing(self):
        rows = [{'id':'a','arrival_s':1.25,'tokens':[1332,23748,1332],
                 'nested':{'big':2**90,'negative':-5000,'bool':True,'none':None}},
                {'id':'b','tokens':[1332,23748,1332]}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'trace.jsonl'
            path.write_text('\n'+''.join(json.dumps(r)+'\n' for r in rows))
            for limit in (0,1,65536):
                got = load_trace(path,cache_limit=limit)
                self.assertEqual(got,rows)
                self.assertIs(type(got[0]['tokens']),list)
                self.assertIs(type(got[0]['nested']['bool']),bool)
                self.assertIs(type(got[0]['nested']['big']),int)
                got[0]['tokens'][0]=999
                self.assertEqual(got[1]['tokens'][0],1332)

    def test_malformed_record_remains_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'trace.jsonl';path.write_text('{"x":123}\n{"bad":\n')
            with self.assertRaises(json.JSONDecodeError):load_trace(path)


if __name__ == '__main__':unittest.main()
