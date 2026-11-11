"""Separate process protocol for user-authored Python nodes (not a security sandbox)."""
import contextlib
import io
import json
import sys
import traceback

class BoundedText(io.StringIO):
    def write(self, text):
        available = max(0, 128000 - self.tell())
        super().write(text[:available])
        return len(text)

payload = json.load(sys.stdin)
stdout, stderr = BoundedText(), BoundedText()
namespace = dict(payload['inputs'])
namespace['__name__'] = '__pipeline__'
result = {}
try:
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exec(compile(payload['code'], '<pipeline-node>', 'exec'), namespace)
    values = {name: namespace[name] for name in payload['outputs'] if name in namespace}
    json.dumps(values, allow_nan=False)
    result = {'values': values, 'error': None}
except BaseException:
    result = {'values': {}, 'error': traceback.format_exc()[-16000:]}
result.update(stdout=stdout.getvalue(), stderr=stderr.getvalue())
json.dump(result, sys.stdout, ensure_ascii=False, allow_nan=False)
