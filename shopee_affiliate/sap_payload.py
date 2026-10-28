"""Independent serializer for the observed single-root SAP FlatBuffer.

The caller must supply already-derived field values and exact insertion order.
This recreates the byte layout, not the fingerprint collectors or field derivation.
Only the one-root, byte-vector/string, scalar schema observed in this SDK is supported.
"""
from dataclasses import dataclass, replace
import math
import struct


@dataclass(frozen=True)
class _Builder:
    data: bytes = b''
    alignment: int = 1

    def prepend(self, value: bytes):
        return replace(self, data=value+self.data)

    def prepare(self, alignment: int, additional: int = 0):
        padding = (-(len(self.data)+additional)) & (alignment-1)
        return replace(self, data=b'\0'*padding+self.data,
                       alignment=max(self.alignment, alignment))

    def integer(self, value: int, width: int):
        return self.prepare(width).prepend((value & ((1 << (width*8))-1)).to_bytes(width,'little'))

    def float32(self, value: float):
        return self.prepare(4).prepend(struct.pack('<f', value))

    def offset(self, target: int):
        ready = self.prepare(4)
        return ready.prepend((len(ready.data)-target+4).to_bytes(4,'little'))

    def vector(self, data: bytes, is_string: bool):
        ready = self.integer(0,1) if is_string else self
        ready = ready.prepare(4,len(data)).prepare(1,len(data)).prepend(data)
        return ready.prepend(len(data).to_bytes(4,'little'))


def _vector_inputs(vectors):
    if not isinstance(vectors, (list,tuple)) or len(vectors)>57:
        raise ValueError('Invalid vectors')
    result = []
    total_size = 0
    for item in vectors:
        if not isinstance(item,dict) or set(item)!={'kind','hex'} or item['kind'] not in ('bytes','string'):
            raise ValueError('Invalid vector')
        if not isinstance(item['hex'], str) or len(item['hex']) > 131070:
            raise ValueError('Invalid vector encoding length')
        try:
            data=bytes.fromhex(item['hex'])
        except (ValueError,TypeError):
            raise ValueError('Invalid vector encoding') from None
        total_size += len(data) + 5
        if total_size > 65535:
            raise ValueError('Vector too long')
        result.append((data,item['kind']=='string'))
    return result


def _field_inputs(fields, vector_count):
    if not isinstance(fields,(list,tuple)) or len(fields)>57:
        raise ValueError('Invalid fields')
    seen=set()
    result=[]
    for item in fields:
        if not isinstance(item,dict) or set(item)!={'index','kind','value'}:
            raise ValueError('Invalid field')
        index,kind,value=item['index'],item['kind'],item['value']
        if not isinstance(kind, str):
            raise ValueError('Invalid field kind')
        if type(index) is not int or not 0<=index<57 or index in seen:
            raise ValueError('Invalid field index')
        seen.add(index)
        if kind=='offset':
            valid=type(value) is int and 0<=value<vector_count
        elif kind=='float32':
            valid=type(value) in (int,float) and abs(value)<=3.4028234663852886e38 and math.isfinite(value)
        else:
            limits={'uint8':(0,255),'uint16':(0,65535),'int32':(-2147483648,2147483647)}
            valid=kind in limits and type(value) is int and limits[kind][0]<=value<=limits[kind][1]
        if not valid:
            raise ValueError('Invalid field value')
        result.append((index,kind,value))
    return result


def build_payload(vectors, fields, *, rotation: int = 0, renderer_placeholder: bool = False) -> bytes:
    """Encode observed SAP schema inputs without JavaScript or a browser.

    `vectors` contains {kind: bytes|string, hex: ...}; `fields` contains
    {index: 0..56, kind: uint8|uint16|int32|float32|offset, value: ...}.
    An offset value is a zero-based vector index. Order is significant for
    byte-for-byte SDK parity. Use rotation=0 for already-rotated indices, or
    canonical indices with the value from derive_slot_rotation(ri).
    """
    if type(rotation) is not int or not 0 <= rotation < 57:
        raise ValueError('Invalid field rotation')
    vector_inputs=_vector_inputs(vectors)
    if type(renderer_placeholder) is not bool or renderer_placeholder and len(vector_inputs) < 9:
        raise ValueError('Invalid renderer placeholder state')
    field_inputs=_field_inputs(fields,len(vector_inputs))
    builder=_Builder()
    targets=[]
    for index, (data,is_string) in enumerate(vector_inputs):
        if index == 8 and renderer_placeholder:
            builder = builder.vector(b'', True)
        builder=builder.vector(data,is_string)
        targets.append(len(builder.data))
    object_start=len(builder.data)
    slots=(0,)*57
    for index,kind,value in field_inputs:
        index = (index + rotation) % 57
        if kind!='offset' and value==0:
            continue
        if kind=='offset': builder=builder.offset(targets[value])
        elif kind=='float32': builder=builder.float32(value)
        else: builder=builder.integer(value,{'uint8':1,'uint16':2,'int32':4}[kind])
        slots=slots[:index]+(len(builder.data),)+slots[index+1:]
    builder=builder.integer(0,4)
    root=len(builder.data)
    last=max((i for i,x in enumerate(slots) if x),default=-1)
    for offset in reversed(slots[:last+1]):
        builder=builder.integer(root-offset if offset else 0,2)
    builder=builder.integer(root-object_start,2).integer((last+3)*2,2)
    at=len(builder.data)-root
    builder=replace(builder,data=builder.data[:at]+(len(builder.data)-root).to_bytes(4,'little')+builder.data[at+4:])
    builder=builder.prepare(builder.alignment,4).offset(root)
    if len(builder.data)>65535:
        raise ValueError('Payload too long')
    return builder.data
