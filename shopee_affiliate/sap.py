"""Offline pure-Python reconstruction of recovered SAP framing.

This CLI requires an explicitly supplied serialized payload. It does not collect
fingerprints, mint tokens, call Shopee, or generate a payload from cookies.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from .sap_codec import decode_ri, decode_sec, encode_sec, inspect_flatbuffer
from .sap_payload import build_payload

MAX_INPUT_BYTES = 128 * 1024


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate field')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('invalid constant')


def _read_input(path: Path | None) -> dict:
    if path is None:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        with path.open('rb') as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError('input size')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                       parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError('input shape')
    return value


def reconstruct(value: dict) -> dict[str, str]:
    """Rebuild RI/SEC for explicit payload bytes, not for a product ID."""
    if set(value) != {'ri', 'payload_base64'}:
        raise ValueError('input fields')
    info = decode_ri(value['ri'])
    text = value['payload_base64']
    if not isinstance(text, str):
        raise ValueError('payload encoding')
    payload = base64.b64decode(text, validate=True)
    if base64.b64encode(payload).decode('ascii') != text:
        raise ValueError('payload encoding')
    return {'x-sap-ri': info.raw.hex(), 'x-sap-sec': encode_sec(payload, info.xor_key)}


def inspect(value: dict) -> dict:
    if set(value) != {'ri', 'sec'}:
        raise ValueError('input fields')
    info = decode_ri(value['ri'])
    sec = decode_sec(value['sec'], value['ri'])
    result = {'version': sec.version, 'checksum_valid': True,
              'payload_bytes': len(sec.payload), 'timestamp_seconds': info.timestamp_seconds,
              'ri_protocol_version': info.protocol_version}
    try:
        layout = inspect_flatbuffer(sec.payload)
        result['flatbuffer'] = {'root_offset': layout['root_offset'],
            'field_count': len(layout['field_offsets']), 'object_size': layout['object_size']}
    except ValueError:
        result['flatbuffer'] = None
    return result


def build(value: dict) -> dict[str, str]:
    """Serialize explicit derived fields and frame them; no field collection."""
    if set(value) != {'ri', 'vectors', 'fields'}:
        raise ValueError('input fields')
    info = decode_ri(value['ri'])
    payload = build_payload(value['vectors'], value['fields'])
    return {'x-sap-ri': info.raw.hex(), 'x-sap-sec': encode_sec(payload, info.xor_key)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('reconstruct', 'build', 'inspect'))
    parser.add_argument('--input', type=Path, help='Read JSON from a local file; default stdin')
    args = parser.parse_args(argv)
    try:
        value = _read_input(args.input)
        output = {'reconstruct': reconstruct, 'build': build, 'inspect': inspect}[args.command](value)
        print(json.dumps(output))
        return 0
    except (ValueError, TypeError, OverflowError, RecursionError, OSError):
        print('SAP input is invalid, unsupported, corrupt, or unreadable.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
