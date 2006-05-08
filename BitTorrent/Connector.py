# The contents of this file are subject to the BitTorrent Open Source License
# Version 1.1 (the License).  You may not copy or use this file, in either
# source code or executable form, except in compliance with the License.  You
# may obtain a copy of the License at http://www.bittorrent.com/license/.
#
# Software distributed under the License is distributed on an AS IS basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.  See the License
# for the specific language governing rights and limitations under the
# License.

# Originally written by Bram Cohen, heavily modified by Uoti Urpala
# Fast and cache extensions added by David Harrison

# DEBUG
# If you think FAST_EXTENSION is causing problems then set the following:
#disable_fast_extension = True
disable_fast_extension = False
# END DEBUG

from __future__ import generators

# for crypto
from os import urandom
from random import randrange
from sha import sha
from Crypto.Cipher import ARC4

from struct import pack, unpack

from BitTorrent.RawServer_twisted import Handler
from BitTorrent.bitfield import Bitfield
from BitTorrent.obsoletepythonsupport import *
import logging


def toint(s):
    return unpack("!i", s)[0]

def tobinary(i):
    return pack("!i", i)

CHOKE = chr(0)
UNCHOKE = chr(1)
INTERESTED = chr(2)
NOT_INTERESTED = chr(3)
# index
HAVE = chr(4)
# index, bitfield
BITFIELD = chr(5)
# index, begin, length
REQUEST = chr(6)
# index, begin, piece
PIECE = chr(7)
# index, begin, piece
CANCEL = chr(8)

# 2-byte port message
PORT = chr(9)

# no args
#GET_METAINFO =   chr(10)
#GIVE_METAINFO =  chr(11)

# index
#SUSPECT_PIECE =  chr(12)

# no args
SUGGEST_PIECE =  chr(13)
HAVE_ALL =       chr(14)
HAVE_NONE =      chr(15)

# index, begin, length
REJECT_REQUEST = chr(16)

# index
ALLOWED_FAST =   chr(17)


message_dict = {chr(0):'CHOKE',
                chr(1):'UNCHOKE',
                chr(2):'INTERESTED',
                chr(3):'NOT_INTERESTED',
                chr(4):'HAVE',
                chr(5):'BITFIELD',
                chr(6):'REQUEST',
                chr(7):'PIECE',
                chr(8):'CANCEL',
                chr(9):'PORT',
                #chr(10): 'GET_METAINFO',    # proposed CACHE_EXTENSION
                #chr(11): 'GIVE_METAINFO',   # proposed CACHE_EXTENSION
                #chr(12): 'SUSPECT_PIECE',   # proposed CACHE_EXTENSION
                chr(13): 'SUGGEST_PIECE',   # proposed FAST_EXTENSION
                chr(14): 'HAVE_ALL',        # proposed FAST_EXTENSION
                chr(15): 'HAVE_NONE',       # proposed FAST_EXTENSION
                chr(16): 'REJECT_REQUEST',  # proposed FAST_EXTENSION
                chr(17): 'ALLOWED_FAST'     # proposed FAST_EXTENSION
                }

# reserved flags:
#  reserved[0]
#   0x80 Azureus Messaging Protocol
#  reserved[5]
#   0x10 uTorrent extensions: peer exchange, encrypted connections,
#       broadcast listen port.
#  reserved[7]
DHT = 0x01
CACHE_EXTENSION = 0x02  # caching extensions.
FAST_EXTENSION = 0x04   # suggest, haveall, havenone, reject request,
                        # and allow fast extensions.

# Cache extension is currently disabled.
#FLAGS = '\0' * 7 + chr( DHT | CACHE_EXTENSION | FAST_EXTENSION )
if disable_fast_extension:
    FLAGS = '\0' * 7 + chr( DHT )
else:
    FLAGS = '\0' * 7 + chr( DHT | FAST_EXTENSION )
protocol_name = 'BitTorrent protocol'

# for crypto
dh_prime = 0xFFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A63A36210000000000090563
PAD_MAX = 200 # less than protocol maximum, and later assumed to be < 256
DH_BYTES = 96
def bytetonum(x):
    return long(x.encode('hex'), 16)
def numtobyte(x):
    x = hex(x).lstrip('0x').rstrip('Ll')
    x = '0'*(192 - len(x)) + x
    return x.decode('hex')
  
noisy = False
#noisy = True
if noisy:
    connection_logger = logging.getLogger("BitTorrent.Connector")
    log = connection_logger.debug


def protocol_violation(s, c=None):
    a = ''
    if noisy:
        if c is not None:
            a = (c.ip, c.port)
        log( "FAUX PAS: %s %s" % ( s, a ))

# Dave's comments: Connection is a bad name. 

class Connection(Handler):
    """Implements the syntax of the BitTorrent protocol. 
       See Upload.py and Download.py for the connection-level 
       semantics."""

    def __init__(self, parent, connection, id, is_local,
                 obfuscate_outgoing=False):
        self.parent = parent
        self.connection = connection
        self.id = id
        self.ip = connection.ip
        self.locally_initiated = is_local
        self.complete = False
        self.closed = False
        self.got_anything = False
        self.next_upload = None
        self.upload = None
        self.download = None
        self._buffer = []
        self._buffer_len = 0
        self._reader = self._read_messages()
        self._next_len = self._reader.next()
        self._partial_message = None
        self._outqueue = []
        self._decrypt = None
        self._privkey = None        
        self.choke_sent = True
        self.uses_dht = False
        self.uses_cache_extension = False
        self.uses_fast_extension = False
        self.dht_port = None
        self.sloppy_pre_connection_counter = 0
        self.received_data = False
        if self.locally_initiated:
            if obfuscate_outgoing:
                privkey = bytetonum(urandom(20))
                self._privkey = privkey
                pubkey = pow(2, privkey, dh_prime)
                out = numtobyte(pubkey) + urandom(randrange(PAD_MAX))
                connection.write(out)
            else:
                connection.write(chr(len(protocol_name)) + protocol_name +
                                 FLAGS + self.parent.download_id)
                if self.id is not None:
                    connection.write(self.parent.my_id)
        # Greg's comments: ow ow ow
        self.connection.handler = self


    def set_parent(self, parent):
        self.parent = parent

    def close(self):
        if not self.closed:
            self.connection.close()

    def send_interested(self):
        if noisy:
            log( "SEND %s" % message_dict[INTERESTED] )
        self._send_message(INTERESTED)

    def send_not_interested(self):
        if noisy:
            log( "SEND %s" % message_dict[NOT_INTERESTED] )
        self._send_message(NOT_INTERESTED)

    def send_choke(self):
        if self._partial_message is None:
            if noisy:
                log( "SEND %s" % message_dict[CHOKE] )
            self._send_message(CHOKE)
            self.choke_sent = True
            self.upload.sent_choke()

    def send_unchoke(self):
        if self._partial_message is None:
            if noisy:
                log( "SEND %s" % message_dict[UNCHOKE] )
            self._send_message(UNCHOKE)
            self.choke_sent = False

    def send_port(self, port):
        if noisy:
            log( "SEND %s" % message_dict[PORT] )
        self._send_message(PORT+pack('!H', port))
        
    def send_request(self, index, begin, length):
        if noisy:
            log( "SEND %s %d %d %d" % (message_dict[REQUEST], index, begin, length) )
        self._send_message(pack("!ciii", REQUEST, index, begin, length))

    def send_cancel(self, index, begin, length):
        self._send_message(pack("!ciii", CANCEL,index, begin, length))

    def send_bitfield(self, bitfield):
        if noisy:
            log( "SEND %s" % message_dict[BITFIELD] )
        self._send_message(BITFIELD + bitfield)

    def send_have(self, index):
        if noisy:
            log( "SEND %s" % message_dict[HAVE] )
        self._send_message(pack("!ci", HAVE, index))

    def send_have_all(self):
        assert(self.uses_fast_extension)
        if noisy:
            log( "SEND %s" % message_dict[HAVE_ALL] )
        self._send_message(pack("!c", HAVE_ALL))

    def send_have_none(self):
        assert(self.uses_fast_extension)
        if noisy:
            log( "SEND %s" % message_dict[HAVE_NONE] )
        self._send_message(pack("!c", HAVE_NONE))

    def send_reject_request(self, index, begin, length):
        assert(self.uses_fast_extension)
        self._send_message(pack("!ciii", REJECT_REQUEST,index,begin,length))

    def send_allowed_fast(self, index):
        assert(self.uses_fast_extension)
        self._send_message(pack("!ci", ALLOWED_FAST, index ))

    def send_keepalive(self):
        self._send_message('')

    #def send_get_metainfo(self):
    #    assert(self.uses_cache_extension)
    #    self._send_message(pack("!c", GET_METAINFO))

    # There is some question as to whether this should be implemented
    # as a single message that contains the entire metainfo as this
    # could result in a particularly large file.  --D. Harrison
    #def send_give_metainfo(self, metainfo):
    #    pass

    def send_partial(self, bytes):
        if self.closed:
            return 0
        if self._partial_message is None and not self.upload.buffer:
            return 0
        if self._partial_message is None:
            total = 0
            self._partial_message = []
            while self.upload.buffer and total < bytes:
                t, piece = self.upload.buffer.pop(0)
                index, begin, length = t
                msg = pack("!icii%ss" % len(piece), len(piece) + 9, PIECE,
                           index, begin, piece)
                if noisy: log( "SEND PIECE %d %d" % (index,begin) )
                self._partial_message.append(msg)
                total += len(msg)
            self._partial_message = ''.join(self._partial_message)
        if bytes < len(self._partial_message):
            self.upload.update_rate(bytes)
            self.connection.write(buffer(self._partial_message, 0, bytes))
            self._partial_message = buffer(self._partial_message, bytes)
            return bytes
        queue = [str(self._partial_message)]
        self._partial_message = None
        if self.choke_sent != self.upload.choked:
            if self.upload.choked:
                self._outqueue.append(pack("!ic", 1, CHOKE))
                self.upload.sent_choke()
            else:
                self._outqueue.append(pack("!ic", 1, UNCHOKE))
            self.choke_sent = self.upload.choked
        queue.extend(self._outqueue)
        self._outqueue = []
        queue = ''.join(queue)
        self.upload.update_rate(len(queue))
        self.connection.write(queue)
        return len(queue)

    # yields the number of bytes it wants next, gets those in self._message
    def _read_messages(self):

        # be compatible with encrypted clients. Thanks Uoti        
        yield 1 + len(protocol_name)
        if self._privkey is not None or \
           self._message != chr(len(protocol_name)) + protocol_name:
            if self.locally_initiated:
                if self._privkey is None:
                    return
                dhstr = self._message
                yield DH_BYTES - len(dhstr)
                dhstr += self._message
                pub = bytetonum(dhstr)
                S = numtobyte(pow(pub, self._privkey, dh_prime))
                pub = self._privkey = dhstr = None
                SKEY = self.parent.download_id
                x = sha('req3' + S).digest()
                streamid = sha('req2'+SKEY).digest()
                streamid = ''.join([chr(ord(streamid[i]) ^ ord(x[i]))
                                    for i in range(20)])
                encrypt = ARC4.new(sha('keyA' + S + SKEY).digest()).encrypt
                encrypt('x'*1024)
                padlen = randrange(PAD_MAX)
                x = sha('req1' + S).digest() + streamid + encrypt(
                    '\x00'*8 + '\x00'*3+'\x02'+'\x00'+chr(padlen)+
                    urandom(padlen)+'\x00\x00')
                self.connection.write(x)
                self.connection.encrypt = encrypt
                decrypt = ARC4.new(sha('keyB' + S + SKEY).digest()).decrypt
                decrypt('x'*1024)
                VC = decrypt('\x00'*8) # actually encrypt
                x = ''
                while 1:
                    yield 1
                    x += self._message
                    i = (x + self._rest).find(VC)
                    if i >= 0:
                        break
                    yield len(self._rest)
                    x += self._message
                    if len(x) >= 520:
                        protocol_violation('VC not found',
                                           self.connection)
                        return
                yield i + 8 + 4 + 2 - len(x)
                x = decrypt((x + self._message)[-6:])
                self._decrypt = decrypt
                if x[0:4] != '\x00\x00\x00\x02':
                    protocol_violation('bad crypto method selected, not 2',
                                       self.connection)
                    return
                padlen = (ord(x[4]) << 8) + ord(x[5])
                if padlen > 512:
                    protocol_violation('padlen too long',
                                       self.connection)
                    return
                self.connection.write(chr(len(protocol_name)) + protocol_name +
                                      FLAGS + self.parent.download_id)
                yield padlen
            else:
                dhstr = self._message
                yield DH_BYTES - len(dhstr)
                dhstr += self._message
                privkey = bytetonum(urandom(20))
                pub = numtobyte(pow(2, privkey, dh_prime))
                self.connection.write(pub + urandom(randrange(PAD_MAX)))
                pub = bytetonum(dhstr)
                S = numtobyte(pow(pub, privkey, dh_prime))
                dhstr = pub = privkey = None
                streamid = sha('req1' + S).digest()
                x = ''
                while 1:
                    yield 1
                    x += self._message
                    i = (x + self._rest).find(streamid)
                    if i >= 0:
                        break
                    yield len(self._rest)
                    x += self._message
                    if len(x) >= 532:
                        protocol_violation('incoming VC not found',
                                           self.connection)
                        return
                yield i + 20 + 20 + 8 + 4 + 2 - len(x)
                self._message = (x + self._message)[-34:]
                streamid = self._message[0:20]
                x = sha('req3' + S).digest()
                streamid = ''.join([chr(ord(streamid[i]) ^ ord(x[i]))
                                    for i in range(20)])
                self.parent.select_torrent_obfuscated(self, streamid)
                if self.parent.download_id is None:
                    protocol_violation('download id unknown/rejected',
                                       self.connection)
                    return
                SKEY = self.parent.download_id
                decrypt = ARC4.new(sha('keyA' + S + SKEY).digest()).decrypt
                decrypt('x'*1024)
                s = decrypt(self._message[20:34])
                if s[0:8] != '\x00' * 8:
                    protocol_violation('BAD VC', self.connection)
                    return
                crypto_provide = toint(s[8:12])
                padlen = (ord(s[12]) << 8) + ord(s[13])
                if padlen > 512:
                    protocol_violation('BAD padlen, too long', self.connection)
                    return
                self._decrypt = decrypt
                yield padlen + 2
                s = self._message
                encrypt = ARC4.new(sha('keyB' + S + SKEY).digest()).encrypt
                encrypt('x'*1024)
                self.connection.encrypt = encrypt
                if not crypto_provide & 2:
                    protocol_violation("peer doesn't support crypto mode 2", self.connection)
                    return
                padlen = randrange(PAD_MAX)
                s = '\x00' * 11 + '\x02\x00' + chr(padlen) + urandom(padlen)
                self.connection.write(s)
            S = SKEY = s = x = streamid = VC = padlen = None
            yield 1 + len(protocol_name)
            if self._message != chr(len(protocol_name)) + protocol_name:
                protocol_violation('classic handshake fails', self.connection)
                return

        yield 8  # reserved
        # dht is on last reserved byte
        if ord(self._message[7]) & DHT:
            self.uses_dht = True
        if ord(self._message[7]) & CACHE_EXTENSION:
            if noisy: log( "Implements CACHE_EXTENSION")
            self.uses_cache_extension = True
        if ord(self._message[7]) & FAST_EXTENSION:
            if disable_fast_extension:
                self.uses_fast_extension = False
            else:
                if noisy: log( "Implements FAST_EXTENSION")
                self.uses_fast_extension = True
        
        yield 20 # download id
        if self.parent.download_id is None:  # incoming connection
            # modifies self.parent if successful
            self.parent.select_torrent(self, self._message)
            if self.parent.download_id is None:
                protocol_violation("no download_id from parent (peer from a torrent you're not running)", self.connection)
                return
        elif self._message != self.parent.download_id:
            protocol_violation("incorrect download_id from parent",
                               self.connection)
            return
        if not self.locally_initiated:
            self.connection.write(chr(len(protocol_name)) + protocol_name +
                FLAGS + self.parent.download_id + self.parent.my_id)

        yield 20  # peer id
        if not self.id:
            self.id = self._message
            if self.id == self.parent.my_id:
                protocol_violation("talking to self", self.connection)
                return
            for v in self.parent.connections.itervalues():
                if v is not self:
                    if v.id == self.id:
                        protocol_violation(
                            "duplicate connection (id collision)",
                            self.connection)
                        return
                    if self.parent.config['one_connection_per_ip'] and \
                           v.ip == self.ip:
                        protocol_violation(
                            "duplicate connection (ip collision)",
                            self.connection)
                        return
            if self.locally_initiated:
                self.connection.write(self.parent.my_id)
            else:
                self.parent.everinc = True
        else:
            if self._message != self.id:
                protocol_violation("incorrect id")
                return
        self.complete = True
        self.parent.connection_completed(self)

        while True:
            yield 4   # message length
            l = toint(self._message)
            if l > self.parent.config['max_message_length']:
                protocol_violation("message length exceeds max (%s %s)" %
                    (l, self.parent.config['max_message_length']),
                    self.connection)
                return
            if l > 0:
                yield l
                self._got_message(self._message)

    def _got_message(self, message):
        t = message[0]
        #if noisy: log( "GOT %s" % message_dict[t] )
        if t in [BITFIELD, HAVE_ALL, HAVE_NONE] and self.got_anything:
            self.close()
            return
        self.got_anything = True
        if (t in [CHOKE, UNCHOKE, INTERESTED, NOT_INTERESTED] and
                len(message) != 1):
            self.close()
            return
        if t == CHOKE:
            if noisy: log( "GOT %s" % message_dict[t] )
            self.download.got_choke()
        elif t == UNCHOKE:
            if noisy: log( "GOT %s" % message_dict[t] )
            self.download.got_unchoke()
        elif t == INTERESTED:
            if noisy: log( "GOT %s" % message_dict[t] )
            self.upload.got_interested()
        elif t == NOT_INTERESTED:
            if noisy: log( "GOT %s" % message_dict[t] )
            self.upload.got_not_interested()
        elif t == HAVE:
            i = unpack("!xi", message)[0]
            if noisy: log( "GOT HAVE %d" % i )
            if i >= self.parent.numpieces:
                self.close()
                return
            self.download.got_have(i)
        elif t == BITFIELD:
            try:
                b = Bitfield(self.parent.numpieces, message[1:])
            except ValueError:
                self.close()
                return
            self.download.got_have_bitfield(b)
        elif t == REQUEST:
            if len(message) != 13:
                self.close()
                return
            i, a, b = unpack("!xiii", message)
            if noisy: log( "GOT  REQUEST %d %d %d" % (i, a, b) )
            if i >= self.parent.numpieces:
                self.close()
                return
            self.upload.got_request(i, a, b)
        elif t == CANCEL:
            if len(message) != 13:
                self.close()
                return
            i, a, b = unpack("!xiii", message)
            if noisy: log( "GOT  CANCEL %d %d %d" % (i, a, b) )
            if i >= self.parent.numpieces:
                self.close()
                return
            self.upload.got_cancel(i, a, b)
        elif t == PIECE:
            if len(message) <= 9:
                self.close()
                return
            n = len(message) - 9
            i, a, b = unpack("!xii%ss" % n, message)
            if noisy: log( "GOT PIECE %d %d" % (i, a) )
            if i >= self.parent.numpieces:
                self.close()
                return
            self.download.got_piece(i, a, b)
        elif t == PORT:
            if len(message) != 3:
                self.close()
                return
            self.dht_port = unpack('!H', message[1:3])[0]
            self.parent.got_port(self)
        elif t == SUGGEST_PIECE:
            if not self.uses_fast_extension:
                self.close()
                return
            i = unpack("!xi", message)[0]
            if noisy: log( "GOT SUGGEST_PIECE %d" % i )
            if i >= self.parent.numpieces:
                self.close()
                return
            self.download.got_suggest_piece(i)
        elif t == HAVE_ALL:
            if noisy: log( "GOT %s" % message_dict[t] )
            if not self.uses_fast_extension:
                self.close()
                return
            self.download.got_have_all()
        elif t == HAVE_NONE:
            if noisy: log( "GOT %s" % message_dict[t] )
            if not self.uses_fast_extension:
                self.close()
                return
            self.download.got_have_none()
        elif t == REJECT_REQUEST:
            if not self.uses_fast_extension:
                self.close()
                return
            if len(message) != 13:
                self.close()
                return
            i, a, b = unpack("!xiii", message)
            if noisy: log( "GOT REJECT_REQUEST %d %d" % (i,a) )
            if i >= self.parent.numpieces:
                self.close()
                return
            self.download.got_reject_request(i, a, b)
        elif t == ALLOWED_FAST:
            if not self.uses_fast_extension:
                self.close()
                return
            i = unpack("!xi", message)[0]
            if noisy: log( "GOT ALLOWED_FAST %d" % i )
            self.download.got_allowed_fast(i)
        else:
            self.close()

    def _send_message(self, message):
        if self.closed:
            return
        s = tobinary(len(message)) + message
        if self._partial_message is not None:
            self._outqueue.append(s)
        else:
            self.connection.write(s)

    def data_came_in(self, conn, s):
        self.received_data = True
        if not self.download:
            # this is really annoying.
            self.sloppy_pre_connection_counter += len(s)
        else:
            l = self.sloppy_pre_connection_counter + len(s)
            self.sloppy_pre_connection_counter = 0
            
        while True:
            if self.closed:
                return
            i = self._next_len - self._buffer_len
            if i > len(s):
                self._buffer.append(s)
                self._buffer_len += len(s)
                return
            m = s[:i]
            if self._buffer_len > 0:
                self._buffer.append(m)
                m = ''.join(self._buffer)
                self._buffer = []
                self._buffer_len = 0
            s = s[i:]
            if self._decrypt is not None:
                m = self._decrypt(m)
            self._message = m
            self._rest = s
            try:
                self._next_len = self._reader.next()
            except StopIteration:
                self.close()
                return

    def connection_lost(self, conn):
        assert conn is self.connection
        self.closed = True
        self._reader = None
        del self.parent.connections[self.connection]
        # ARG. Thanks Uoti
        if hasattr(self.parent, 'ratelimiter'):
            self.parent.ratelimiter.dequeue(self)
        if self.locally_initiated and not self.received_data:
            dns = (self.connection.ip, self.connection.port)
            self.parent.start_connection(dns, id=None, encrypt=True)
        self.connection = None
        self.parent.replace_connection()
        if self.complete:
            self.parent.complete_connections.remove(self)
            if self.download is not None: self.download.disconnected()
            self.parent.choker.connection_lost(self)
            self.upload = self.download = None

    def connection_flushed(self, connection):
        if self.complete and self.next_upload is None and (self._partial_message is not None
                                             or (self.upload and self.upload.buffer)):
                self.parent.ratelimiter.queue(self)


