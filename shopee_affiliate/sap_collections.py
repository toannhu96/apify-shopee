"""Typed collector layouts recovered from captured SDK writer call sites."""
import math
import struct
from zlib import adler32

_LAYOUTS = {
    'custom': (
        (38787, 'u16', True, None, True),
        (39420, 'u16', True, None, True),
        (41507, 'u8', False, None, True),
        (37604, 'f32', False, None, True),
        (41766, 'u8', False, None, True),
        (45193, 'u8', False, None, True),
        (36280, 'u32', True, None, True),
        (38475, 'u32', False, None, True),
        (36354, 'u32', False, None, True),
        (28519, 'u8', False, None, True),
        (35751, 'length', False, 15273, True),
        (35764, 'blob', False, None, True),
        (44304, 'u8', False, None, True),
        (43483, 'u8', False, None, True),
        (36428, 'u32', True, None, True),
        (35931, 'u32', False, None, True),
        (44517, 'length', False, 4910, True),
        (44530, 'blob', False, None, True),
        (35314, 'length', False, 46154, True),
        (35327, 'blob', False, None, False),
        (43833, 'u8', False, None, True),
        (37834, 'u32', False, None, True),
        (28360, 'u8', False, None, True),
        (36594, 'length', True, 11570, True),
        (36607, 'blob', False, None, True),
        (37057, 'u32', False, None, True),
        (35403, 'length', False, 31974, True),
        (35416, 'blob', False, None, False),
        (39277, 'u16', True, None, True),
        (37468, 'length', True, 61953, True),
        (37481, 'blob', False, None, True),
        (29285, 'u8', False, None, True),
        (43619, 'u8', False, None, True),
        (29061, 'u8', False, None, True),
        (43368, 'u8', False, None, True),
        (29760, 'u8', False, None, True),
        (44727, 'u8', False, None, True),
        (29989, 'u8', False, None, True),
        (43022, 'u8', False, None, True),
        (39726, 'length', False, 51993, True),
        (39739, 'blob', False, None, True),
        (29689, 'u8', False, None, True),
        (42951, 'u8', False, None, True),
        (44251, 'length', True, 63135, True),
        (44264, 'blob', False, None, True),
        (36662, 'u8', False, None, False),
        (41043, 'u8', False, None, True),
        (40542, 'u8', False, None, True),
        (40278, 'u32', True, None, True),
        (43921, 'u8', False, None, True),
        (37678, 'u16', True, None, True),
        (39196, 'u8', False, None, False),
        (35451, 'u32', True, None, True),
        (39175, 'u8', False, None, False),
        (37752, 'u16', True, None, True),
        (42798, 'u16', False, None, True),
        (29902, 'u8', False, None, True),
        (38393, 'u32', False, None, True),
        (42485, 'u16', False, None, True),
        (29448, 'u8', False, None, True),
    ),
    'salsa': (
        (30060, 'u8', False, None, True),
        (29377, 'u8', False, None, True),
        (44047, 'u8', False, None, True),
        (29563, 'u8', False, None, True),
        (36744, 'u32', False, None, True),
        (30282, 'u8', False, None, True),
        (42880, 'u8', False, None, True),
        (39856, 'u16', False, None, True),
        (41166, 'u8', False, None, True),
        (44798, 'u8', False, None, True),
        (38644, 'u32', True, None, True),
        (41932, 'u16', False, None, True),
        (45111, 'u16', True, None, True),
        (37975, 'length', False, 19124, True),
        (37988, 'blob', False, None, True),
        (40808, 'u16', False, None, True),
        (35838, 'u32', False, None, True),
        (36817, 'u32', False, None, True),
        (35982, 'u32', False, None, True),
        (40009, 'length', False, 20089, True),
        (40022, 'blob', False, None, True),
        (37224, 'u32', False, None, True),
        (43762, 'u8', False, None, True),
        (36122, 'length', False, 5, True),
        (36135, 'blob', False, None, False),
        (36975, 'u32', True, None, True),
        (39112, 'u16', True, None, True),
        (38311, 'u32', True, None, True),
        (28289, 'u8', False, None, True),
        (42098, 'u16', False, None, True),
        (41384, 'u8', False, None, True),
        (41634, 'u8', False, None, True),
        (40382, 'u8', False, None, True),
        (39011, 'u16', False, None, True),
        (42264, 'u16', False, None, True),
        (39560, 'u32', False, None, True),
        (30538, 'u8', False, None, True),
        (38930, 'u16', True, None, True),
        (44603, 'u32', False, None, True),
        (45351, 'u32', False, None, True),
        (29831, 'u8', False, None, True),
        (40702, 'u8', False, None, True),
    ),
}


def _layout(kind):
    if not isinstance(kind, str) or kind not in _LAYOUTS:
        raise ValueError("Unsupported collector kind")
    return [{"pc":pc,"type":typ,"little":little,"length_xor":length_xor,"masked":masked}
            for pc,typ,little,length_xor,masked in _LAYOUTS[kind]]

def parse_collection(kind, plaintext):
    layout = _layout(kind)
    if not isinstance(plaintext, bytes) or len(plaintext) > 65535:
        raise ValueError('Expected bounded collector bytes')
    cursor = 0 if kind == 'custom' else len(plaintext)
    fields = []
    blob_length = None
    for item in layout:
        width = blob_length if item['type'] == 'blob' else {
            'u8': 1, 'u16': 2, 'u32': 4, 'f32': 4, 'length': 2}[item['type']]
        if width is None:
            raise ValueError('Missing blob length')
        start = cursor if kind == 'custom' else cursor - width
        if start < 0 or start + width > len(plaintext):
            raise ValueError('Collector field exceeds input')
        raw = plaintext[start:start + width]
        order = 'little' if item.get('little') else 'big'
        if item['type'] == 'blob':
            value = raw
        elif item['type'] == 'f32':
            value = struct.unpack('<f' if item.get('little') else '>f', raw)[0]
            if not math.isfinite(value) or value < 0:
                raise ValueError('Collector request rate must be finite and nonnegative')
        else:
            value = int.from_bytes(raw, order)
        if item['type'] == 'length':
            blob_length = value ^ item['length_xor']
        fields.append({**item, 'width': width, 'value': value})
        cursor = start + width if kind == 'custom' else start
    if cursor != (len(plaintext) if kind == 'custom' else 0):
        raise ValueError('Collector contains trailing bytes')
    return fields


def serialize_collection(kind, fields):
    layout = _layout(kind)
    if not isinstance(fields, (list, tuple)) or len(fields) != len(layout):
        raise ValueError('Expected all collector fields')
    chunks = []
    for item, expected in zip(fields, layout):
        if not isinstance(item, dict) or any(item.get(k) != v for k, v in expected.items()):
            raise ValueError('Collector field does not match schema')
        value = item['value']
        order = 'little' if item.get('little') else 'big'
        if item['type'] == 'blob':
            if not isinstance(value, bytes) or len(value) > 65535:
                raise ValueError('Expected bounded collector field bytes')
            raw = value
        elif item['type'] == 'f32':
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 3.4028234663852886e38:
                raise ValueError('Collector request rate must be finite nonnegative float32')
            raw = struct.pack('<f' if item.get('little') else '>f', value)
        else:
            width = {'u8': 1, 'u16': 2, 'u32': 4, 'length': 2}[item['type']]
            if item.get('width') != width or type(value) is not int or not 0 <= value < 1 << (8 * width):
                raise ValueError('Invalid collector scalar')
            raw = value.to_bytes(item['width'], order)
        chunks.append(raw)
    result = b''.join(chunks if kind == 'custom' else reversed(chunks))
    parse_collection(kind, result)
    return result


def collection_request_state(plaintext: bytes, request_mask: bytes) -> dict:
    """Decode source-defined request count, elapsed whole seconds and rate."""
    if not isinstance(request_mask, bytes) or len(request_mask) != 8:
        raise ValueError('Expected eight-byte request mask')
    fields = {item['pc']: item['value'] for item in parse_collection('custom', plaintext)}
    mask32 = int.from_bytes(request_mask[:4], 'little')
    return {'path_count': fields[37057] ^ mask32 ^ 3947457725,
            'elapsed_seconds': fields[36280] ^ mask32 ^ 3270953752,
            'request_rate': fields[37604]}


def complete_response_collection(plaintext: bytes, request_mask: bytes) -> bytes:
    """Apply SDK7116's received-response Xad/XaR callbacks for this path.

    Both fetch resolution and XHR readyState4 increment global VFC and path
    VVc, without checking HTTP status or response body. Transport failures do
    not imply receipt. These are separate from the request counters.
    """
    if not isinstance(request_mask, bytes) or len(request_mask) != 8:
        raise ValueError('Expected eight-byte request mask')
    mask = int.from_bytes(request_mask[:4], 'little')
    constants = {36428: 2441765646, 37834: 4276226149}
    refreshed = []
    for item in parse_collection('custom', plaintext):
        value = item['value']
        if item['pc'] in constants:
            constant = constants[item['pc']]
            count = value ^ mask ^ constant
            if count == 0xffffffff:
                raise ValueError('Response counter overflow')
            value = (count + 1) ^ mask ^ constant
        refreshed.append({**item, 'value': value})
    return serialize_collection('custom', refreshed)


def refresh_collection(kind: str, plaintext: bytes, context) -> bytes:
    """Rekey captured device facts and update request RI and URL-derived fields.

    Preserves SDK startup time and device identity. Optional advance_request
    advances the four original SDK counters for another request on the same path.
    Binary layout and known-answer parity are tied to SDK8568.e933698b.
    """
    from .sap_codec import decode_ri, xxh32
    old, new = context.old_mask, context.new_mask
    if any(not isinstance(key, bytes) or len(key) != 8 for key in (old, new)):
        raise ValueError('Expected eight-byte request masks')
    if not isinstance(context.new_random, bytes) or len(context.new_random) != 8:
        raise ValueError('Expected eight request random bytes')
    if not isinstance(context.raw_url, str) or len(context.raw_url) > 8192:
        raise ValueError('Expected bounded request URL text')
    decode_ri(context.new_ri)
    fields = parse_collection(kind, plaintext)
    advance = getattr(context, 'advance_request', False)
    requested_rate = getattr(context, 'request_rate', None)
    elapsed_seconds = getattr(context, 'elapsed_seconds', None)
    if type(advance) is not bool:
        raise ValueError('advance_request must be boolean')
    if advance and (type(requested_rate) not in (float, int) or
                    not math.isfinite(requested_rate) or not 0 <= requested_rate <= 3.4028234663852886e38):
        raise ValueError('Advancing request counters requires an explicit finite request_rate')
    if not advance and requested_rate is not None:
        raise ValueError('request_rate requires advance_request')
    if advance and (type(elapsed_seconds) is not int or not 0 <= elapsed_seconds <= 0xffffffff):
        raise ValueError('Advancing request state requires explicit uint32 elapsed_seconds')
    if not advance and elapsed_seconds is not None:
        raise ValueError('elapsed_seconds requires advance_request')
    counter_constants = {37057: 3947457725, 36744: 3882804305,
                         35838: 604268431, 37224: 1471809388}
    advance_xhr = getattr(context, 'advance_xhr', False)
    if type(advance_xhr) is not bool or (advance_xhr and not advance):
        raise ValueError('advance_xhr requires advance_request')
    if advance_xhr:
        # SDK7116 increments module3023.XaQ before signing each XHR send.
        counter_constants = {**counter_constants, 36975: 3663584677}
    old_u32 = int.from_bytes(old[:4], 'little')
    new_u32 = int.from_bytes(new[:4], 'little')
    refreshed = []
    for item in fields:
        value = item['value']
        if advance and item['pc'] in counter_constants:
            constant = counter_constants[item['pc']]
            count = value ^ old_u32 ^ constant
            if count == 0xffffffff:
                raise ValueError('Request counter overflow')
            value = (count + 1) ^ new_u32 ^ constant
        elif advance and item['pc'] == 37604:
            value = requested_rate
        elif advance and item['pc'] == 36280:
            value = elapsed_seconds ^ new_u32 ^ 3270953752
        elif item['pc'] == 35931:
            value = (xxh32(context.new_ri.encode('ascii')) ^
                     int.from_bytes(new[:4], 'little') ^ 3284639437)
        elif item['pc'] == 35327:
            value = format(adler32(context.raw_url.encode('utf8') +
                                   context.new_random[4:]), 'x').encode('ascii')
        elif item['type'] == 'blob' and item.get('masked'):
            value = bytes(b ^ old[i % 8] ^ new[i % 8] for i, b in enumerate(value))
        elif item['type'] in ('u8', 'u16', 'u32') and item.get('masked', True):
            width = item['width']
            value ^= int.from_bytes(old[:width], 'little') ^ int.from_bytes(new[:width], 'little')
        refreshed.append({**item, 'value': value})
    result = [{**item, 'value': len(refreshed[i+1]['value']) ^ item['length_xor']}
              if item['type'] == 'length' else item
              for i, item in enumerate(refreshed)]
    return serialize_collection(kind, result)
