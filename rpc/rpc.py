#!/usr/bin/env python3

import os
import binascii
import threading
import time
import struct
import itertools
import signal
import ipaddress

def asn_int4(val):
    return b'\x02\x04' + struct.pack('>L', val)

class XMMRPC(object):
    def __init__(self, path='/dev/xmm0/rpc'):
        self.fp = os.open(path, os.O_RDWR | os.O_SYNC)

        self.callbacks = {}
        self.call_acknowledged = set()
        self.response_event = threading.Event()
        self.response = None
        self._stop = False
        self.reader_thread = threading.Thread(target=self.reader)
        self.reader_thread.start()

        # loop over 1..255, excluding 0
        self.tid_gen = itertools.cycle(range(1, 256))

    def __del__(self):
        self.stop()
        os.close(self.fp)

    def execute(self, cmd, body=asn_int4(0), callback=None, is_async=False):
        if is_async and callback is None:
            waiter = threading.Event()
            def callback(code, body):
                self.response = code, body
                waiter.set()
        else:
            waiter = None

        if callback:
            tid = 0x11000100 | next(self.tid_gen)
        else:
            tid = 0

        tid_word = 0x11000100 | tid

        if tid:
            self.callbacks[tid_word] = callback

        total_length = len(body) + 16
        if tid:
            total_length += 6
        header = struct.pack('<L', total_length) + asn_int4(total_length) + asn_int4(cmd) + struct.pack('>L', tid_word)
        if tid:
            header += asn_int4(tid)

        assert total_length + 4 == len(header) + len(body)

        self.response_event.clear()
        ret = os.write(self.fp, header + body)
        if ret < len(header + body):
            print("write error: %d", ret)
        self.response_event.wait()

        # Async response without callback = block until self.response updated
        if waiter:
            waiter.wait()

        return self.response

    def handle_message(self, message):
        length = message[:4]
        len1_p = message[4:10]
        code_p = message[10:16]
        txid = message[16:20]
        body = message[20:]

        assert len1_p.startswith(b'\x02\x04')
        assert code_p.startswith(b'\x02\x04')

        l0 = struct.unpack('<L', length)[0]
        l1 = struct.unpack('>L', len1_p[2:])[0]
        code = struct.unpack('>L', code_p[2:])[0]
        txid = struct.unpack('>L', txid)[0]

        if l0 != l1:
            print("length mismatch, framing error?")

        if txid == 0:
            print('unsolicited: %04x: %s' % (code, binascii.hexlify(body)))
        elif txid == 0x11000100:
            print('%04x: %s' % (code, binascii.hexlify(body)))
            self.response_event.set()
            self.response = (code, body)
        else:
            if txid not in self.callbacks:
                print('unexpected txid %08x' % txid)
                return

            if txid not in self.call_acknowledged:
                print('tx %08x acknowledged' % txid)
                self.call_acknowledged.add(txid)
                self.response = (code, body)
                self.response_event.set()
            else:
                print('tx %08x completed' % txid)
                self.call_acknowledged.remove(txid)
                # response payload always starts with tid; rip it off
                cb_thread = threading.Thread(target=self.callbacks[txid], args=(code, body[6:]))
                cb_thread.start()
                self.callbacks.pop(txid)

    def reader(self):
        while not self._stop:
            dd = os.read(self.fp, 32768)
            self.handle_message(dd)

    def stop(self):
        self._stop = True
        # interrupt os.read()
        os.kill(os.getpid(), signal.SIGALRM)

def _pack_string(val, fmt, elem_type):
    length_str = ''
    while len(fmt) and fmt[0].isdigit():
        length_str += fmt.pop(0)

    length = int(length_str)
    assert len(val) <= length
    valid = len(val)

    elem_size = len(struct.pack(elem_type, 0))
    field_type = {1: 0x55, 2: 0x56, 4: 0x57}[elem_size]
    payload = struct.pack('%d%s' % (valid, elem_type), *val)

    count = length * elem_size
    padding = (length - valid) * elem_size

    if valid < 128:
        valid_field = struct.pack('B', valid)
    else:
        remain = valid
        valid_field = [0x80]
        while remain > 0:
            valid_field[0] += 1
            valid_field.insert(1, remain & 0xff)
            remain >>= 8

    field = struct.pack('B', field_type)
    field += bytes(valid_field)
    field += pack('LL', count, padding)
    field += payload
    field += b'\0'*padding
    return field

def take_asn_int(data):
    assert data.pop(0) == 0x02
    l = data.pop(0)
    val = 0
    for i in range(l):
        val <<= 8
        val |= data.pop(0)
    return val

def unpack(fmt, data):
    data = bytearray(data)
    out = []
    for ch in fmt:
        if ch == 'n':
            val = take_asn_int(data)
            out.append(val)
        elif ch == 's':
            t = data.pop(0)
            assert t in [0x55, 0x56, 0x57]
            valid = data.pop(0)
            if valid & 0x80:    # Variable length!
                value = 0
                for byte in range(valid & 0xf): # lol
                    value |= data.pop(0) << (byte*8)
                valid = value
            if t == 0x56:
                valid <<= 1
            elif t == 0x57:
                valid <<= 2
            count = take_asn_int(data)   # often equals valid + padding, but sometimes not
            padding = take_asn_int(data)
            if count:
                assert count == (valid + padding)
                field_size = count
            else:
                field_size = valid
            payload = data[:valid]
            data = data[valid + padding:]
            out.append(payload)
        else:
            raise ValueError("unknown format char %s" % ch)

    return out

def pack(fmt, *args):
    out = b''
    fmt = list(fmt)
    args = list(args)

    while len(fmt):
        arg = args.pop(0)
        ch = fmt.pop(0)

        if ch == 'B':
            out += b'\x02\x01' + struct.pack('B', arg)
        elif ch == 'H':
            out += b'\x02\x02' + struct.pack('>H', arg)
        elif ch == 'L':
            out += b'\x02\x04' + struct.pack('>L', arg)
        elif ch == 's':
            out += _pack_string(arg, fmt, 'B')
        elif ch == 'S':
            elem_type = fmt.pop(0)
            out += _pack_string(arg, fmt, elem_type)
        else:
            raise ValueError("Unknown format char '%s'" % ch)

    if len(args):
        raise ValueError("Too many args supplied")

    return out

def bytes_to_ipv4(data):
    return ipaddress.IPv4Address(int(binascii.hexlify(data), 16))

def bytes_to_ipv6(data):
    return ipaddress.IPv6Address(int(binascii.hexlify(data), 16))

def pack_UtaMsCallPsAttachApnConfigReq(apn):
    apn_string = bytearray(101)
    apn_string[:len(apn)] = apn.encode('ascii')

    args = [0, b'\0'*257, 0, b'\0'*65, b'\0'*65, b'\0'*250, 0, b'\0'*250, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, b'\0'*20, 0, b'\0'*101, b'\0'*257, 0, b'\0'*65, b'\0'*65, b'\0'*250, 0, b'\0'*250, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, b'\0'*20, 0, b'\0'*101, b'\0'*257, 0, b'\0'*65, b'\0'*65, b'\0'*250, 0, b'\0'*250, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0x404, 1, 0, 1, 0, 0, b'\0'*20, 3, apn_string, b'\0'*257, 0, b'\0'*65, b'\0'*65, b'\0'*250, 0, b'\0'*250, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0x404, 1, 0, 1, 0, 0, b'\0'*20, 3, apn_string, 3, 0,]

    types = 'Bs260Ls66s65s250Bs252HLLLLLLLLLLLLLLLLLLLLLs20Ls104s260Ls66s65s250Bs252HLLLLLLLLLLLLLLLLLLLLLs20Ls104s260Ls66s65s250Bs252HLLLLLLLLLLLLLLLLLLLLLs20Ls104s260Ls66s65s250Bs252HLLLLLLLLLLLLLLLLLLLLLs20Ls103BL'
    return pack(types, *args)

def pack_UtaMsNetAttachReq():
    return pack('BLLLLHHLL', 0, 0, 0, 0, 0, 0xffff, 0xffff, 0, 0)

def pack_UtaMsCallPsGetNegIpAddrReq():
    return pack('BLL', 0, 0, 0)

def unpack_UtaMsCallPsGetNegIpAddrReq(data):
    _, addresses, _, _, _, _ = unpack('nsnnnn', data)
    a1 = bytes_to_ipv4(addresses[:4])
    a2 = bytes_to_ipv4(addresses[4:8])
    a3 = bytes_to_ipv4(addresses[8:12])

    return a1, a2, a3


def pack_UtaMsCallPsGetNegotiatedDnsReq():
    return pack('BLL', 0, 0, 0)

def unpack_UtaMsCallPsGetNegotiatedDnsReq(data):
    v4 = []
    v6 = []
    vals = unpack('n' + 'sn'*16 + 'nsnnnn', data)
    for i in range(16):
        address, typ = vals[2*i+1:2*i+3]
        if typ == 1:
            v4.append(bytes_to_ipv4(address[:4]))
        elif typ == 2:
            v6.append(bytes_to_ipv6(address[:16]))

    return {'v4': v4, 'v6': v6}


def pack_UtaMsCallPsConnectReq():
    return pack('BLLL', 0, 6, 0, 0)

def pack_UtaRPCPsConnectToDatachannelReq(path='/sioscc/PCIE/IOSM/IPS/0'):
    bpath = path.encode('ascii') + b'\0'
    return pack('s24', bpath)



if __name__ == "__main__":
    rpc = XMMRPC()

    from rpc_unpack_table import rpc_unpack_table
    for k, v in rpc_unpack_table.items():
        locals()[v] = k

    fcc_status = rpc.execute(CsiFccLockQueryReq, is_async=True)
    print("fcc status: %s" % binascii.hexlify(fcc_status[1]))

    rpc.execute(UtaMsSmsInit)
    rpc.execute(UtaMsCbsInit)
    rpc.execute(UtaMsNetOpen)
    rpc.execute(UtaMsCallCsInit)
    rpc.execute(UtaMsCallPsInitialize)
    rpc.execute(UtaMsSsInit)
    rpc.execute(UtaMsSimOpenReq)
    rpc.stop()
