"""Decode the recovered rotated 57-field SAP FlatBuffer; no network or SDK needed.

Returns raw transformed fields, not a full fingerprint interpretation.
Values can be sensitive when used with real headers; this module never logs them.
"""
import math
import re
import struct

FIELD_ORDER = ((54, 'uint16', None), (22, 'int32', None), (8, 'int32', None), (3, 'uint16', None), (14, 'uint8', None), (40, 'offset', 11), (7, 'offset', 0), (38, 'uint8', None), (49, 'uint8', None), (12, 'offset', 2), (1, 'uint8', None), (21, 'uint16', None), (46, 'uint16', None), (15, 'uint8', None), (19, 'uint8', None), (48, 'uint8', None), (6, 'uint16', None), (50, 'offset', 12), (2, 'uint8', None), (25, 'uint8', None), (10, 'offset', 14), (32, 'uint8', None), (27, 'uint8', None), (45, 'uint8', None), (11, 'uint8', None), (53, 'uint8', None), (43, 'int32', None), (0, 'uint16', None), (13, 'offset', 1), (18, 'offset', 13), (24, 'uint8', None), (28, 'int32', None), (31, 'offset', 3), (52, 'offset', 5), (36, 'offset', 15), (20, 'uint8', None), (26, 'uint8', None), (37, 'uint8', None), (56, 'offset', 8), (30, 'uint16', None), (5, 'float32', None), (42, 'uint8', None), (33, 'uint8', None), (44, 'offset', 9), (29, 'uint16', None), (41, 'int32', None), (47, 'offset', 4), (51, 'offset', 10), (9, 'uint16', None), (23, 'uint16', None), (16, 'uint8', None), (17, 'offset', 6), (39, 'int32', None), (4, 'uint8', None), (35, 'int32', None), (34, 'uint8', None), (55, 'offset', 7))
VECTOR_KINDS = ('string', 'bytes', 'bytes', 'string', 'bytes', 'string', 'string', 'string', 'string', 'string', 'string', 'bytes', 'bytes', 'bytes', 'bytes', 'bytes')


def _unpack(payload, fmt, position):
    size=struct.calcsize(fmt)
    if type(position) is not int or position<0 or position+size>len(payload):
        raise ValueError('Invalid payload bounds')
    return struct.unpack_from(fmt,payload,position)[0]


def _reserve_range(existing, start, end):
    if any(start < old_end and old_start < end for old_start, old_end in existing):
        raise ValueError('Overlapping payload allocations')
    return existing + ((start, end),)


def decode_payload(payload: bytes, ri: str) -> dict:
    """Recover exact builder inputs from payload and its matching RI header.

    Rebuild with build_payload(result['vectors'], result['fields'],
    renderer_placeholder=result['renderer_placeholder']). This decoder supports
    this pinned SDK schema with distinct, aligned field/vector allocations.
    """
    if not isinstance(payload,bytes) or not 8<=len(payload)<=65535:
        raise ValueError('Invalid payload size')
    if not isinstance(ri,str) or not re.fullmatch('[0-9a-fA-F]{52}',ri):
        raise ValueError('Invalid RI encoding')
    request=bytes.fromhex(ri)
    rotation=(int.from_bytes(request[:4],'little')+request[22]+request[23])%57
    root=_unpack(payload,'<I',0)
    if root<8 or root%4:raise ValueError('Invalid root offset')
    back=_unpack(payload,'<i',root)
    vtable=root-back
    if back<=0 or vtable<4 or vtable%2:raise ValueError('Invalid vtable offset')
    table_size=_unpack(payload,'<H',vtable)
    object_size=_unpack(payload,'<H',vtable+2)
    if table_size<4 or table_size>118 or table_size%2 or vtable+table_size>root or object_size<4 or root+object_size>len(payload):
        raise ValueError('Invalid table size')
    field_ranges=()
    vector_ranges=()
    vector_results={}
    vector_positions={}
    fields=[]
    canonical={}
    formats={'uint8':'<B','uint16':'<H','int32':'<i','float32':'<f','offset':'<I'}
    for index,kind,vector_index in FIELD_ORDER:
        slot=(index+rotation)%57
        relative=_unpack(payload,'<H',vtable+4+slot*2) if 4+slot*2<table_size else 0
        if relative:
            width=struct.calcsize(formats[kind])
            if relative<4 or relative+width>object_size or (root+relative)%width:
                raise ValueError('Invalid field offset')
            field_ranges=_reserve_range(field_ranges,root+relative,root+relative+width)
            value=_unpack(payload,formats[kind],root+relative)
        else:value=0
        if kind=='offset':
            if not relative or value<4:raise ValueError('Missing vector field')
            target=root+relative+value
            if target%4:raise ValueError('Invalid vector alignment')
            length=_unpack(payload,'<I',target)
            start=target+4
            end=start+length
            if target<root+object_size or end>len(payload):raise ValueError('Invalid vector bounds')
            if VECTOR_KINDS[vector_index]=='string' and (end>=len(payload) or payload[end]!=0):
                raise ValueError('Invalid string terminator')
            allocation_end=end+(VECTOR_KINDS[vector_index]=='string')
            vector_ranges=_reserve_range(vector_ranges,target,allocation_end)
            vector_results[vector_index]={'kind':VECTOR_KINDS[vector_index],'hex':payload[start:end].hex()}
            vector_positions[vector_index] = (target, end)
            value=vector_index
        elif kind=='float32' and not math.isfinite(value):
            raise ValueError('Invalid float field')
        fields.append({'index':slot,'kind':kind,'value':value})
        canonical[index]={'kind':kind,'value':value}
    renderer_gap = vector_positions[7][0] - ((vector_positions[8][1] + 4) & ~3)
    if renderer_gap not in (0, 8):
        raise ValueError('Unsupported renderer allocation layout')
    return {'rotation':rotation,'renderer_placeholder': renderer_gap == 8,
            'vectors':[vector_results[i]for i in range(len(VECTOR_KINDS))],
            'fields':fields,'canonical_fields':canonical}
