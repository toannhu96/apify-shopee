"""Preserve captured scalar state while changing its request-key obfuscation.

This does not generate missing browser state or refresh its age-dependent meaning.
Original SDK pairs verified all 57 canonical fields, including special hash field43.
"""
import math
from collections.abc import Callable
from dataclasses import dataclass
from zlib import adler32

from .sap_codec import decode_ri, decode_sec, encode_sec
from .sap_crypto import decode_collection, encode_collection
from .sap_detector import decode_detectors, encode_detectors
from .sap_payload import build_payload
from .sap_request import (
    derive_slot_rotation,
    encode_request_timestamp,
    request_url_digest,
    xor_repeating,
)
from .sap_state import FIELD_ORDER, decode_payload


def _key(value):
    if not isinstance(value,bytes) or len(value)!=8:
        raise ValueError('Expected an eight-byte request key')
    return value


def rekey_scalar_fields(canonical_fields: dict, old_key: bytes, new_key: bytes) -> dict:
    old_key,new_key=_key(old_key),_key(new_key)
    if not isinstance(canonical_fields,dict) or set(canonical_fields)!=set(range(57)):
        raise ValueError('Expected all canonical fields')
    result={}
    for index,kind,vector_index in FIELD_ORDER:
        item=canonical_fields[index]
        if not isinstance(item,dict) or set(item)!={'kind','value'} or item['kind']!=kind:
            raise ValueError('Invalid canonical field')
        value=item['value']
        if kind=='float32':
            if type(value) not in (float,int) or abs(value)>3.4028234663852886e38 or not math.isfinite(value):
                raise ValueError('Invalid scalar value')
        elif type(value)is not int:
            raise ValueError('Invalid scalar value')
        if kind=='offset':
            if value!=vector_index:raise ValueError('Invalid vector index')
        elif kind!='float32':
            width={'uint8':1,'uint16':2,'int32':4}[kind]
            limit=(1<<(width*8))-1
            if not (-(1<<31)<=value<(1<<31) if kind=='int32' else 0<=value<=limit):
                raise ValueError('Invalid scalar range')
            if index==43:
                value=adler32(new_key)^3084883394
            elif index!=34:
                value=value^int.from_bytes(old_key[:width],'little')^int.from_bytes(new_key[:width],'little')
            value&=limit
            if kind=='int32' and value>=1<<31:value-=1<<32
        result[index]={'kind':kind,'value':value}
    return result


@dataclass(frozen=True)
class RefreshContext:
    """Explicit request inputs passed to the required collection refresher."""
    old_mask: bytes
    new_mask: bytes
    old_random: bytes
    new_random: bytes
    raw_url: str
    timestamp_ms: int
    old_ri: str
    new_ri: str
    advance_request: bool = False
    request_rate: float | None = None
    elapsed_seconds: int | None = None
    root_request_rate: float | None = None
    advance_xhr: bool = False


def _refresh_vectors(old, context, refresh_collection, detector_padding):
    vectors = tuple(bytes.fromhex(v['hex']) for v in old['vectors'])
    replacements = {
        0: request_url_digest(context.raw_url, context.new_random[4:]),
        1: context.new_mask,
        2: encode_detectors(decode_detectors(vectors[2], context.old_mask),
                            context.new_mask, detector_padding),
        4: encode_request_timestamp(context.timestamp_ms, context.new_mask),
        14: context.new_random,
    }
    strings = {i: xor_repeating(xor_repeating(vectors[i], context.old_mask),
                               context.new_mask) for i in (3, 5, 6, 7, 8, 9, 10)}
    collections = {}
    for data_index, checksum_index, kind in ((12, 13, 'custom'), (15, 11, 'salsa')):
        plaintext = decode_collection(vectors[data_index], vectors[checksum_index],
                                      kind, context.old_mask, context.old_random)
        refreshed = refresh_collection(kind, plaintext, context)
        if not isinstance(refreshed, bytes) or len(refreshed) > 65535:
            raise ValueError('Collection refresher must return bounded bytes')
        data, checksum = encode_collection(refreshed, kind, context.new_mask,
                                           context.new_random)
        collections = {**collections, data_index: data, checksum_index: checksum}
    replacements = {**replacements, **strings, **collections}
    return [{'kind': v['kind'], 'hex': replacements[i].hex()}
            for i, v in enumerate(old['vectors'])]


def _refresh_fields(old, context, vectors):
    canonical = rekey_scalar_fields(old['canonical_fields'], context.old_mask,
                                   context.new_mask)
    if context.advance_request:
        canonical = {**canonical, 5: {'kind': 'float32', 'value': context.root_request_rate}}
    if context.advance_xhr:
        # SDK3023.VVr(location.href): XHR requests from the captured page.
        field = canonical[28]
        mask = int.from_bytes(context.new_mask[:4], 'little')
        count = (field['value'] & 0xffffffff) ^ mask ^ 2959024693
        if count == 0xffffffff:
            raise ValueError('XHR page counter overflow')
        value = (count + 1) ^ mask ^ 2959024693
        canonical = {**canonical, 28: {'kind': 'int32',
            'value': value if value < 2**31 else value - 2**32}}
    mask16 = int.from_bytes(context.new_mask[:2], 'little')
    canonical = {**canonical,
        9: {'kind': 'uint16', 'value': len(bytes.fromhex(vectors[12]['hex'])) ^ mask16 ^ 47270},
        29: {'kind': 'uint16', 'value': len(bytes.fromhex(vectors[15]['hex'])) ^ mask16 ^ 61861}}
    return [{'index': i, 'kind': kind, 'value': canonical[i]['value']}
            for i, kind, _ in FIELD_ORDER]


def _validate_request_rate(advance_request, request_rate, elapsed_seconds, root_request_rate):
    if type(advance_request) is not bool:
        raise ValueError('Invalid advance_request flag')
    if not advance_request:
        if request_rate is not None:
            raise ValueError('A request rate requires advance_request=True')
    elif (type(request_rate) not in (float, int)
          or not 0 <= request_rate <= 3.4028234663852886e38
          or not math.isfinite(request_rate)):
        raise ValueError('A finite nonnegative float32 request rate is required')
    if advance_request:
        if type(elapsed_seconds) is not int or not 0 <= elapsed_seconds < 2**32:
            raise ValueError('Explicit uint32 elapsed_seconds is required')
    elif elapsed_seconds is not None:
        raise ValueError('elapsed_seconds requires advance_request=True')
    if advance_request:
        if (type(root_request_rate) not in (float, int)
                or not 0 <= root_request_rate <= 3.4028234663852886e38
                or not math.isfinite(root_request_rate)):
            raise ValueError('Explicit finite nonnegative float32 root_request_rate is required')
    elif root_request_rate is not None:
        raise ValueError('root_request_rate requires advance_request=True')


def refresh_root(old_sec: str, old_ri: str, new_ri: str, *, new_mask: bytes,
                 new_random: bytes, raw_url: str, timestamp_ms: int,
                 refresh_collection: Callable[[str, bytes, RefreshContext], bytes],
                 detector_padding: bytes | None = None, advance_request: bool = False,
                 request_rate: float | None = None, elapsed_seconds: int | None = None,
                 root_request_rate: float | None = None,
                 advance_xhr: bool = False) -> dict[str, str]:
    """Assemble a refreshed root using a required semantic collection callback.

    Callback receives kind ('custom'/'salsa'), verified decrypted plaintext and
    immutable context. It must parse/rekey fields and update request dependencies.
    Device facts and origin are preserved. When advance_request=True, a caller
    must supply source-derived elapsed_seconds, request_rate (total path rate) and
    root_request_rate (separate 120-second path-window rate for canonical field5).
    No clock or missing window anchor is inferred.
    advance_xhr additionally models SDK7116's XHR send hook: total XHR count
    and requests from the captured page. Standalone generateSignEntry omits it.
    This low-level API accepts explicit masks for SDK parity tests; its caller
    must enforce agreement between mask time, RI time and timestamp_ms.
    """
    if not callable(refresh_collection):
        raise ValueError('A source-derived collection refresh callback is required')  # noqa: TRY004 - stable validation API
    if type(advance_xhr) is not bool or (advance_xhr and not advance_request):
        raise ValueError('advance_xhr requires advance_request=True')
    _validate_request_rate(advance_request, request_rate, elapsed_seconds, root_request_rate)
    new_mask, new_random = _key(new_mask), _key(new_random)
    ri = decode_ri(new_ri)
    old = decode_payload(decode_sec(old_sec, old_ri).payload, old_ri)
    context = RefreshContext(bytes.fromhex(old['vectors'][1]['hex']), new_mask,
        bytes.fromhex(old['vectors'][14]['hex']), new_random, raw_url, timestamp_ms, old_ri, new_ri,
        advance_request, request_rate, elapsed_seconds, root_request_rate, advance_xhr)
    vectors = _refresh_vectors(old, context, refresh_collection, detector_padding)
    fields = _refresh_fields(old, context, vectors)
    payload = build_payload(vectors, fields, rotation=derive_slot_rotation(new_ri),
                            renderer_placeholder=old['renderer_placeholder'])
    return {'x-sap-ri': new_ri, 'x-sap-sec': encode_sec(payload, ri.xor_key)}
