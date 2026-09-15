"""Validate local compose bindings and optionally persist replay endpoints."""
import json
from pathlib import Path
import re
import sys


def connections(vm, ch, pg):
    for binding in (vm, ch, pg):
        match = re.fullmatch(r'127\.0\.0\.1:([0-9]{1,5})', binding)
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ValueError('expected a single allocated loopback port')
    if len({vm, ch, pg}) != 3:
        raise ValueError('backend port collision')
    return {'RCA_VM_URL': 'http://' + vm,
            'RCA_CH_URL': 'http://lucida:lucida123@' + ch,
            'RCA_PG_DSN': 'postgres://lucida:lucida123@' + pg + '/lucida?sslmode=disable'}


if __name__ == '__main__':
    result = connections(*sys.argv[1:4])
    if len(sys.argv) > 4 and sys.argv[4]:
        with Path(sys.argv[4]).open('x') as output:
            json.dump(result, output, indent=2)
