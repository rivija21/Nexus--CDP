"""End-to-end security for the BPSK link: Noise KK sessions over the air.

Where it sits
-------------
    bpsk_app.ChatApp
        | plaintext link frames      ('tx', bytes)  /  on_frame(dict)
    SecureLink  (this file)          seals every frame except broadcast IDLE,
        | sealed link frames         runs the handshakes, drops the rest
    radio

LinkState is unchanged in what it sends and expects; it never sees a key.
SecureLink turns each of its frames into an FT_SEALED frame on the way out
and back into the original frame on the way in, after checking it.

Keys and trust
--------------
Every node owns an X25519 key pair (keys/node_<addr>.key, never transmitted).
keys/roster.json lists every node's PUBLIC key by address; it holds nothing
secret and is the same file on every node (see bpsk_keys.py). A node only
talks to addresses whose public key is in its roster.

Handshake: Noise_KK_25519_ChaChaPoly_SHA256
-------------------------------------------
KK = both sides already know each other's static key (from the roster).

    -> e, es, ss      HS_INIT   initiator timestamp, session index, boot id
    <- e, ee, se      HS_RESP   responder session index, boot id

Both sides then hold two fresh 32-byte transport keys (one per direction)
derived from ephemeral-ephemeral DH. The ephemeral keys are discarded, so
recording the air and stealing a node's static key later does not decrypt
old sessions: that is the forward-secrecy property. Sessions are replaced
every REKEY_AFTER seconds, which bounds how much traffic one session key
protects. Mutual authentication comes from es/ss/se: only the holder of the
roster key for an address can complete a handshake as that address.

Handshake frames are not ARQ'd; SecureLink retries them itself with a fresh
ephemeral each time. A responder does not transmit on a new session until the
initiator has used it (the initiator sends a sealed keep-alive at once), so a
replayed HS_INIT cannot move the responder onto a session nobody can finish.
The HS_INIT timestamp must increase per initiator, so an old HS_INIT is
refused outright.

Transport frame (outer link type FT_SEALED, link payload below)
---------------------------------------------------------------
    byte  0      version
    byte  1      origin address       } end-to-end, authenticated. A future
    byte  2      final destination    } relay may rewrite the outer link
    bytes 3-6    receiver's session index   header but not these.
    bytes 7-14   counter, uint64 BE (the nonce; never reused under a key)
    bytes 15..   ChaCha20-Poly1305( inner type | seq | ack | flags | payload )
                 with AAD = bytes 0-14, 16-byte tag at the end

The inner header carries the real frame type and the ARQ fields, so they are
authenticated and an observer cannot tell a chat line from a file fragment or
an ACK except by length. The outer header carries only addresses. Overhead:
35 bytes per frame.

Replay: every transmission is sealed with a new counter, including ARQ
retransmissions. The receiver keeps a 2048-frame sliding window per session
and drops (does not ACK) any counter it has seen. Link-layer duplicate
suppression still handles a retransmission whose first copy arrived.

What is NOT protected
---------------------
Addresses, frame lengths and timing are visible: an observer can tell who is
talking to whom and how much. IDLE filler is broadcast and unauthenticated -
it carries nothing and only keeps the carrier up. A jammer can always deny
service. See README for the full threat model.
"""

import json
import os
import struct
import time

import bpsk_crypto as C
import bpsk_link as L

PROTOCOL_NAME = b'Noise_KK_25519_ChaChaPoly_SHA256'
SEC_VERSION = 1

FT_SEALED = L.FT_SEALED
FT_HS_INIT = L.FT_HS_INIT
FT_HS_RESP = L.FT_HS_RESP
SEC_TYPES = (FT_SEALED, FT_HS_INIT, FT_HS_RESP)

_SEAL = struct.Struct('!BBBIQ')          # version, origin, final, rx index, ctr
_INNER = struct.Struct('!BBBB')          # type, seq, ack, flags
SEAL_OVERHEAD = _SEAL.size + _INNER.size + C.TAG_LEN          # 35 bytes

REKEY_AFTER = 600.0        # initiator starts a new session after this long
REJECT_AFTER = 1800.0      # a session older than this is never used
PREV_GRACE = 30.0          # a replaced session still decrypts for this long
PENDING_TTL = 10.0         # an unconfirmed responder session is dropped
HS_RETRY = 1.0             # first handshake retry interval, seconds
HS_RETRY_MAX = 5.0         # retry interval backs off to this
WINDOW = 2048              # replay window, frames
UNKNOWN_IDX_HOLDOFF = 1.0  # min seconds between handshakes caused by it
RESP_HOLDOFF = 2.0         # don't initiate while answering the peer's handshake
RESYNC_UNKNOWN = 3         # r6.1: frames on an unknown session that force a
#                            new handshake although one is current

FT_IDLE, FT_ACK, FT_BEACON = L.FT_IDLE, L.FT_ACK, L.FT_BEACON
BROADCAST = L.BROADCAST


def fingerprint(pub):
    """Short human-comparable digest of a public key: 'a1b2 c3d4 e5f6 0718'."""
    h = C.sha256(b'bpsk-fp' + pub).hex()[:16]
    return ' '.join(h[i:i + 4] for i in range(0, 16, 4))


def _nonce(n):
    return b'\x00\x00\x00\x00' + struct.pack('<Q', n)


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------
class KeyError_(Exception):
    """Key material missing or inconsistent. Message is operator-facing."""


class KeyStore(object):
    """This node's static key pair and the network roster of public keys.

    keys/node_<addr>.key    64 hex chars, this node's private key, mode 0600
    keys/node_<addr>.state  {"hs_ts": ...} keeps HS_INIT timestamps increasing
                            across restarts even if the clock steps back
    keys/roster.json        {"network": str, "nodes": {"<addr>":
                            {"public_key": hex, "name": str}}}
    """

    def __init__(self, keys_dir, my_addr, create=False):
        self.dir = os.path.abspath(keys_dir)
        self.my_addr = int(my_addr) & 0xFF
        self.key_path = os.path.join(self.dir, 'node_%d.key' % self.my_addr)
        self.state_path = os.path.join(self.dir, 'node_%d.state' % self.my_addr)
        self.roster_path = os.path.join(self.dir, 'roster.json')
        self.created = False
        if not os.path.isfile(self.key_path):
            if not create:
                raise KeyError_('no key for address %d in %s - run: '
                                'python3 bpsk_keys.py init --addr %d'
                                % (self.my_addr, self.dir, self.my_addr))
            os.makedirs(self.dir, exist_ok=True)
            write_private(self.key_path, C.generate_private())
            self.created = True
        self.private = read_private(self.key_path)
        self.public = C.x25519_base(self.private)
        self.network, self.nodes = load_roster(self.roster_path)
        mine = self.nodes.get(self.my_addr)
        if mine is None:
            # Our own public key is not secret; recording it saves a step.
            self.nodes[self.my_addr] = {'public_key': self.public, 'name': ''}
            save_roster(self.roster_path, self.network, self.nodes)
        elif mine['public_key'] != self.public:
            raise KeyError_('roster.json lists a different public key for '
                            'address %d than node_%d.key holds - either the '
                            'roster is stale or this is the wrong key file'
                            % (self.my_addr, self.my_addr))
        self._hs_ts = 0
        try:
            with open(self.state_path) as fh:
                self._hs_ts = int(json.load(fh).get('hs_ts', 0))
        except (OSError, ValueError, TypeError, AttributeError):
            self._hs_ts = 0

    def reload_roster(self):
        self.network, self.nodes = load_roster(self.roster_path)

    def peer_public(self, addr):
        entry = self.nodes.get(int(addr) & 0xFF)
        return entry['public_key'] if entry else None

    def peer_name(self, addr):
        entry = self.nodes.get(int(addr) & 0xFF)
        return (entry or {}).get('name') or ''

    def next_hs_timestamp(self):
        """Strictly increasing, even across restarts and clock corrections."""
        ts = max(time.time_ns(), self._hs_ts + 1)
        self._hs_ts = ts
        try:
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump({'hs_ts': ts}, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            pass
        return ts


class MeshKeyStore(object):
    """r6 key store: this node's key pair plus the set of TRUSTED public keys.

    Addresses are no longer assigned by hand (bpsk_mac derives them from the
    public key), so trust is attached to the key, not to an address:

    keys/node.key       64 hex chars, this node's private key, mode 0600. If
                        it is missing but exactly one r5 node_<addr>.key is
                        present, that key is used (same identity, same
                        fingerprint; nothing is rewritten).
    keys/node.state     {"hs_ts": ..., "addr_n": ...}
    keys/trusted.json   {"network": str, "trusted": {"<public key hex>":
                        {"name": str, "added": unix time}}} - written when the
                        operator approves a node in the UI (or bpsk_keys.py
                        trust). Holds nothing secret.
    keys/roster.json    optional, r5 format; every key in it is trusted too,
                        so a lab can pre-distribute keys and skip approval.

    The address book (address -> public key) is learnt from beacons and lives
    in memory only. peer_public(addr) answers only for a TRUSTED key, which is
    all SecureLink needs: it can neither start nor accept a handshake with a
    node whose key the operator has not approved.
    """

    def __init__(self, keys_dir, create=False):
        self.dir = os.path.abspath(keys_dir)
        self.key_path = os.path.join(self.dir, 'node.key')
        self.state_path = os.path.join(self.dir, 'node.state')
        self.trusted_path = os.path.join(self.dir, 'trusted.json')
        self.roster_path = os.path.join(self.dir, 'roster.json')
        self.created = False
        self.adopted = None
        path = self.key_path
        if not os.path.isfile(path):
            legacy = sorted(n for n in (os.listdir(self.dir)
                                        if os.path.isdir(self.dir) else ())
                            if n.startswith('node_') and n.endswith('.key'))
            if len(legacy) == 1:
                path = self.adopted = os.path.join(self.dir, legacy[0])
            elif not create:
                raise KeyError_('no node key in %s - run: python3 bpsk_keys.py '
                                'init%s' % (self.dir,
                                            '' if not legacy else
                                            ' (several r5 node_<addr>.key '
                                            'files are here; r6 needs one '
                                            'node.key)'))
            else:
                os.makedirs(self.dir, exist_ok=True)
                write_private(self.key_path, C.generate_private())
                self.created = True
        self.private = read_private(path)
        self.public = C.x25519_base(self.private)
        self.my_addr = None
        self.book = {}                      # address -> public key (beacons)
        self.names = {}                     # public key -> nickname (beacons)
        self.reload_trust()
        self._state = {}
        try:
            with open(self.state_path) as fh:
                doc = json.load(fh)
            if isinstance(doc, dict):
                self._state = doc
        except (OSError, ValueError):
            self._state = {}
        try:
            self._hs_ts = int(self._state.get('hs_ts', 0))
        except (TypeError, ValueError):
            self._hs_ts = 0

    # ----------------------------------------------------------- trust
    def reload_trust(self):
        net_r, roster = load_roster(self.roster_path)
        self.roster_keys = {e['public_key']: e.get('name', '')
                            for e in roster.values()}
        self.network, self.trusted = net_r, {}
        if os.path.isfile(self.trusted_path):
            try:
                with open(self.trusted_path) as fh:
                    doc = json.load(fh)
                self.network = str(doc.get('network') or self.network)
                for hexkey, entry in (doc.get('trusted') or {}).items():
                    pub = bytes.fromhex(hexkey)
                    if len(pub) != 32:
                        raise ValueError('a trusted key is not 32 bytes')
                    entry = entry if isinstance(entry, dict) else {}
                    self.trusted[pub] = {'name': str(entry.get('name') or ''),
                                         'added': entry.get('added')}
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise KeyError_('cannot read %s: %s' % (self.trusted_path, exc))

    def _save_trust(self):
        doc = {'network': self.network,
               'trusted': {k.hex(): v for k, v in sorted(self.trusted.items())}}
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.trusted_path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(doc, fh, indent=2)
            fh.write('\n')
        os.replace(tmp, self.trusted_path)

    def is_trusted(self, pub):
        pub = bytes(pub)
        return pub == self.public or pub in self.trusted \
            or pub in self.roster_keys

    def trust(self, pub, name=''):
        pub = bytes(pub)
        if len(pub) != 32:
            raise ValueError('not a 32-byte public key')
        self.trusted[pub] = {'name': str(name or '')[:32],
                             'added': int(time.time())}
        self._save_trust()

    def revoke(self, pub):
        """Returns False if the key is trusted through roster.json instead."""
        pub = bytes(pub)
        found = self.trusted.pop(pub, None) is not None
        if found:
            self._save_trust()
        return pub not in self.roster_keys

    # --------------------------------------------------- address book
    def bind(self, addr, pub, name=None):
        self.book[int(addr) & 0xFF] = bytes(pub)
        if name is not None:
            self.names[bytes(pub)] = name

    def unbind(self, addr):
        return self.book.pop(int(addr) & 0xFF, None)

    def binding(self, addr):
        return self.book.get(int(addr) & 0xFF)

    def peer_public(self, addr):
        pub = self.book.get(int(addr) & 0xFF)
        return pub if pub is not None and self.is_trusted(pub) else None

    def peer_name(self, addr):
        pub = self.book.get(int(addr) & 0xFF)
        if pub is None:
            return ''
        entry = self.trusted.get(pub)
        return ((entry or {}).get('name') or self.roster_keys.get(pub)
                or self.names.get(pub) or '')

    # ---------------------------------------------------------- state
    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def set_state(self, key, value):
        self._state[key] = value
        self._write_state()

    def _write_state(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w') as fh:
                json.dump(self._state, fh)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def next_hs_timestamp(self):
        ts = max(time.time_ns(), self._hs_ts + 1)
        self._hs_ts = ts
        self._state['hs_ts'] = ts
        self._write_state()
        return ts


def write_private(path, private):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(private.hex() + '\n')


def read_private(path):
    with open(path) as fh:
        text = fh.read().strip()
    try:
        key = bytes.fromhex(text)
    except ValueError:
        key = b''
    if len(key) != 32:
        raise KeyError_('%s is not a 32-byte hex private key' % path)
    return key


def load_roster(path):
    if not os.path.isfile(path):
        return 'bpsk-lab', {}
    try:
        with open(path) as fh:
            doc = json.load(fh)
        network = str(doc.get('network') or 'bpsk-lab')
        nodes = {}
        for addr, entry in (doc.get('nodes') or {}).items():
            pub = bytes.fromhex(entry['public_key'])
            if len(pub) != 32:
                raise ValueError('address %s: key is not 32 bytes' % addr)
            nodes[int(addr) & 0xFF] = {'public_key': pub,
                                       'name': str(entry.get('name') or '')}
        return network, nodes
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise KeyError_('cannot read %s: %s' % (path, exc))


def save_roster(path, network, nodes):
    doc = {'network': network,
           'nodes': {str(a): {'public_key': n['public_key'].hex(),
                              'name': n.get('name', '')}
                     for a, n in sorted(nodes.items())}}
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(doc, fh, indent=2)
        fh.write('\n')
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Noise KK
# --------------------------------------------------------------------------
class _Symmetric(object):
    """Noise SymmetricState + CipherState (spec rev. 34, sections 5.1-5.2)."""

    def __init__(self, prologue):
        # The protocol name is exactly HASHLEN (32) bytes, so it is used as-is.
        self.h = PROTOCOL_NAME
        self.ck = self.h
        self.k = None
        self.n = 0
        self.mix_hash(prologue)

    def mix_hash(self, data):
        self.h = C.sha256(self.h + data)

    def mix_key(self, ikm):
        self.ck, self.k = C.hkdf2(self.ck, ikm)
        self.n = 0

    def encrypt_and_hash(self, plaintext):
        ct = (C.aead_encrypt(self.k, _nonce(self.n), plaintext, self.h)
              if self.k is not None else plaintext)
        if self.k is not None:
            self.n += 1
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ct):
        pt = (C.aead_decrypt(self.k, _nonce(self.n), ct, self.h)
              if self.k is not None else ct)
        if self.k is not None:
            self.n += 1
        self.mix_hash(ct)
        return pt

    def split(self):
        return C.hkdf2(self.ck, b'')


def prologue(network, initiator, responder):
    return (b'bpsk-link/%d|' % SEC_VERSION + network.encode('utf-8')
            + b'|' + bytes([initiator & 0xFF, responder & 0xFF]))


def kk_initiator_start(prolog, s_priv, s_pub, rs_pub, payload, e_priv=None):
    """-> e, es, ss. Returns (message, state for kk_initiator_finish)."""
    st = _Symmetric(prolog)
    st.mix_hash(s_pub)
    st.mix_hash(rs_pub)
    e_priv = e_priv or C.generate_private()
    e_pub = C.x25519_base(e_priv)
    st.mix_hash(e_pub)
    st.mix_key(C.x25519(e_priv, rs_pub))            # es
    st.mix_key(C.x25519(s_priv, rs_pub))            # ss
    msg = e_pub + st.encrypt_and_hash(payload)
    return msg, (st, e_priv, s_priv)


def kk_initiator_finish(state, message):
    """<- e, ee, se. Returns (payload, k_send, k_recv, handshake_hash)."""
    st, e_priv, s_priv = state
    if len(message) < 32 + C.TAG_LEN:
        raise C.AuthError('short handshake response')
    re = message[:32]
    st.mix_hash(re)
    st.mix_key(C.x25519(e_priv, re))                # ee
    st.mix_key(C.x25519(s_priv, re))                # se (initiator side)
    payload = st.decrypt_and_hash(message[32:])
    k1, k2 = st.split()
    return payload, k1, k2, st.h


def kk_responder(prolog, s_priv, s_pub, rs_pub, message, payload_fn,
                 e_priv=None):
    """Read -> e, es, ss and write <- e, ee, se.

    payload_fn(initiator_payload) returns the response payload, or raises to
    refuse (e.g. a stale timestamp) - checked BEFORE any response is built.
    Returns (initiator_payload, response, k_send, k_recv, handshake_hash).
    """
    if len(message) < 32 + C.TAG_LEN:
        raise C.AuthError('short handshake initiation')
    st = _Symmetric(prolog)
    st.mix_hash(rs_pub)
    st.mix_hash(s_pub)
    re = message[:32]
    st.mix_hash(re)
    st.mix_key(C.x25519(s_priv, re))                # es (responder side)
    st.mix_key(C.x25519(s_priv, rs_pub))            # ss
    init_payload = st.decrypt_and_hash(message[32:])
    resp_payload = payload_fn(init_payload)
    e_priv = e_priv or C.generate_private()
    e_pub = C.x25519_base(e_priv)
    st.mix_hash(e_pub)
    st.mix_key(C.x25519(e_priv, re))                # ee
    st.mix_key(C.x25519(e_priv, rs_pub))            # se (responder side)
    response = e_pub + st.encrypt_and_hash(resp_payload)
    k1, k2 = st.split()
    return init_payload, response, k2, k1, st.h


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
class _Window(object):
    """Sliding anti-replay window (RFC 6479 idea, bitmap in one Python int)."""

    def __init__(self):
        self.top = -1
        self.bits = 0

    def fresh(self, n):
        if n > self.top:
            return True
        d = self.top - n
        return d < WINDOW and not (self.bits >> d) & 1

    def mark(self, n):
        if n > self.top:
            shift = n - self.top
            self.bits = (((self.bits << shift) | 1) & ((1 << WINDOW) - 1)
                         if shift < WINDOW else 1)
            self.top = n
        else:
            self.bits |= 1 << (self.top - n)


class Session(object):
    __slots__ = ('peer', 'role', 'local_idx', 'remote_idx', 'k_send', 'k_recv',
                 'tx_ctr', 'win', 'created', 'confirmed', 'hash', 'retired')

    def __init__(self, peer, role, local_idx, remote_idx, k_send, k_recv,
                 created, confirmed, hsh):
        self.peer, self.role = peer, role
        self.local_idx, self.remote_idx = local_idx, remote_idx
        self.k_send, self.k_recv = k_send, k_recv
        self.tx_ctr = 0
        self.win = _Window()
        self.created = created
        self.confirmed = confirmed
        self.hash = hsh
        self.retired = None        # time it stopped being current


class _Peer(object):
    def __init__(self, addr):
        self.addr = addr
        self.current = None        # Session used to transmit
        self.previous = None       # still decrypts for PREV_GRACE
        self.pending_resp = None   # responder session awaiting confirmation
        self.init = None           # in-flight initiation: dict
        self.next_try = 0.0
        self.tries = 0
        self.last_init_ts = 0      # highest HS_INIT timestamp accepted
        self.boot_id = None
        self.last_unknown = -1e9
        self.established = 0       # sessions completed with this peer
        self.state = 'idle'
        self.paused = False        # r6: out of range - no new handshakes
        self.unanswered = 0        # r6.1: HS_INITs sent since the last HS_RESP
        self.slow = False          # r6.1: the peer cannot hear us - retry slowly
        self.unknown = 0           # r6.1: frames on sessions we do not hold,
        #                            since our current session was set up


class SecureLink(object):
    """Seals, opens and keys every frame between this node and its peers.

    All entry points return a list of actions like LinkState does:
        ('tx', bytes)   transmit this (already sealed / a handshake frame)
        ('rx', dict)    hand this plaintext frame to LinkState.on_frame
        ('log', str)    operator line
        ('evt', dict)   structured event for the application
    """

    def __init__(self, my_addr, keystore, peers=(), strict=True):
        C.self_test()
        self.my_addr = int(my_addr) & 0xFF
        self.keys = keystore
        self.strict = bool(strict)
        self.peers = {}
        for addr in peers:
            self._peer(int(addr) & 0xFF)
        self.by_idx = {}
        self.boot_id = struct.unpack('!I', os.urandom(4))[0]
        self._last_plain_log = -1e9
        self._last_refused = {}          # origin -> time of the last log line
        # Highest HS_INIT timestamp accepted, per initiator PUBLIC KEY. r5 kept
        # it per address; with r6 self-assigned addresses a node can move, and
        # the replay guard must follow the key, not the address.
        self._init_ts = {}
        # r6.1: round-trip estimate of the radio link (own echo latency plus
        # the transmit backlog), set by the application. Handshake retries
        # wait for twice that instead of a fixed 1 s, so a slow host does not
        # replace an initiation whose answer is still on its way.
        self.rtt = None
        self.stats = dict(sealed_tx=0, opened_rx=0, auth_fail=0, replay=0,
                          unknown_session=0, plain_dropped=0, hs_tx=0,
                          hs_rx=0, hs_fail=0, hs_stale=0, tx_blocked=0,
                          sessions=0)

    # ------------------------------------------------------------ helpers
    def _peer(self, addr):
        p = self.peers.get(addr)
        if p is None:
            p = self.peers[addr] = _Peer(addr)
        return p

    def _new_index(self):
        while True:
            idx = struct.unpack('!I', os.urandom(4))[0]
            if idx and idx not in self.by_idx:
                return idx

    def _drop(self, sess):
        if sess is not None:
            self.by_idx.pop(sess.local_idx, None)

    @staticmethod
    def _state_evt(out, p, state, **kw):
        if p.state != state:
            p.state = state
            out.append(('evt', dict(e='sec_state', peer=p.addr, state=state,
                                    **kw)))

    # ------------------------------------------------- r6: dynamic peers
    def add_peer(self, addr):
        """Start (or resume) keeping a session with addr."""
        p = self._peer(int(addr) & 0xFF)
        p.paused = False
        return p

    def pause(self, addr, paused=True):
        """A peer out of range: keep its sessions, stop sending handshakes."""
        p = self.peers.get(int(addr) & 0xFF)
        if p is not None:
            p.paused = bool(paused)

    def forget(self, addr):
        """Drop everything about addr - it moved to another address, left, or
        its key is no longer trusted. Returns actions."""
        out = []
        p = self.peers.pop(int(addr) & 0xFF, None)
        if p is not None:
            for sess in (p.current, p.previous, p.pending_resp):
                self._drop(sess)
            if p.state != 'idle':
                out.append(('evt', dict(e='sec_state', peer=p.addr,
                                        state='idle')))
        return out

    def set_address(self, addr, now):
        """This node's own address changed. Every session is bound to the old
        one (prologue and sealed header both carry it), so all are dropped and
        rebuilt from the new address."""
        out = []
        self.my_addr = int(addr) & 0xFF
        for p in self.peers.values():
            for sess in (p.current, p.previous, p.pending_resp):
                self._drop(sess)
            p.current = p.previous = p.pending_resp = None
            p.init = None
            p.tries = 0
            p.next_try = now
            if p.state == 'secure':
                self._state_evt(out, p, 'handshaking')
        return out

    def nudge(self, now):
        """The radio just came back on (channel claimed, check finished):
        retry pending handshakes now instead of after their back-off."""
        for p in self.peers.values():
            if p.current is None and not p.paused:
                p.next_try = min(p.next_try, now)

    def ready(self, addr, now=None):
        p = self.peers.get(int(addr) & 0xFF)
        if p is None or p.current is None:
            return False
        now = time.time() if now is None else now
        return (now - p.current.created) < REJECT_AFTER

    def fingerprint(self, addr=None):
        if addr is None:
            return fingerprint(self.keys.public)
        pub = self.keys.peer_public(addr)
        return fingerprint(pub) if pub else None

    # ---------------------------------------------------------- transmit
    def protect(self, raw, now, frm=None):
        """Seal one outgoing link frame. Returns bytes, or None to drop it."""
        if frm is None:
            frm = L.parse(raw)
        if frm['dst'] == BROADCAST and frm['type'] in (FT_IDLE, FT_BEACON):
            # Carrier filler carries nothing. A beacon carries only public
            # information (address, public key, nickname) and is useless to
            # anyone who has not been approved; sessions still prove the key.
            return raw
        p = self.peers.get(frm['dst'])
        sess = p.current if p is not None else None
        if sess is None or (now - sess.created) >= REJECT_AFTER:
            self.stats['tx_blocked'] += 1
            return None
        ctr = sess.tx_ctr
        sess.tx_ctr += 1
        head = _SEAL.pack(SEC_VERSION, self.my_addr, frm['dst'],
                          sess.remote_idx, ctr)
        body = C.aead_encrypt(sess.k_send, _nonce(ctr),
                              _INNER.pack(frm['type'], frm['seq'], frm['ack'],
                                          frm['flags']) + frm['payload'],
                              head)
        self.stats['sealed_tx'] += 1
        return L.build(FT_SEALED, frm['dst'], self.my_addr, 0, 0, head + body,
                       0, ctr)

    # ----------------------------------------------------------- receive
    def accept(self, frm, now):
        """Process one received, CRC-valid link frame (a parse() dict)."""
        out = []
        ftype, dst = frm['type'], frm['dst']
        if dst not in (self.my_addr, BROADCAST):
            out.append(('rx', frm))           # LinkState counts it as not-mine
            return out
        if dst == BROADCAST and ftype in (FT_IDLE, FT_BEACON):
            out.append(('rx', frm))
            return out
        if ftype == FT_SEALED:
            self._open(out, frm, now)
        elif ftype == FT_HS_INIT:
            self._on_init(out, frm, now)
        elif ftype == FT_HS_RESP:
            self._on_resp(out, frm, now)
        elif not self.strict:
            out.append(('rx', frm))
        else:
            self.stats['plain_dropped'] += 1
            if now - self._last_plain_log > 10.0:
                self._last_plain_log = now
                out.append(('log', 'dropped an UNENCRYPTED frame from %d - '
                                   'is that node running r5.2 or with '
                                   'security off?' % frm['src']))
        return out

    def _open(self, out, frm, now):
        pl = frm['payload']
        if len(pl) < _SEAL.size + _INNER.size + C.TAG_LEN:
            self.stats['auth_fail'] += 1
            return
        head = pl[:_SEAL.size]
        ver, origin, final, idx, ctr = _SEAL.unpack(head)
        if ver != SEC_VERSION or final != self.my_addr:
            self.stats['auth_fail'] += 1
            return
        sess = self.by_idx.get(idx)
        if sess is None or sess.peer != origin:
            self.stats['unknown_session'] += 1
            p = self.peers.get(origin)
            if p is not None:
                p.unknown += 1
            # The peer is using a session we do not have - normally because
            # this node restarted. Start a handshake rather than wait for the
            # peer's traffic to time out.
            if (p is not None and self.keys.peer_public(origin)
                    and now - p.last_unknown > UNKNOWN_IDX_HOLDOFF):
                p.last_unknown = now
                if p.init is None:
                    p.next_try = now
            return
        if (now - sess.created) >= REJECT_AFTER:
            self.stats['unknown_session'] += 1
            return
        if not sess.win.fresh(ctr):
            self.stats['replay'] += 1         # seen before: dropped, not ACKed
            return
        try:
            inner = C.aead_decrypt(sess.k_recv, _nonce(ctr), pl[_SEAL.size:],
                                   head)
        except C.AuthError:
            self.stats['auth_fail'] += 1
            return
        sess.win.mark(ctr)
        self.stats['opened_rx'] += 1
        p = self._peer(origin)
        if not sess.confirmed:
            # First use of a responder session by the initiator: it is live.
            sess.confirmed = True
            self._promote(out, p, sess, now)
        itype, seq, ack, flags = _INNER.unpack(inner[:_INNER.size])
        if itype in SEC_TYPES:
            self.stats['auth_fail'] += 1
            return
        out.append(('rx', {'type': itype, 'dst': final, 'src': origin,
                           'seq': seq, 'ack': ack, 'flags': flags,
                           'payload': inner[_INNER.size:], 'sealed': True}))

    def _promote(self, out, p, sess, now):
        """Make sess the transmit session for p; keep the old one briefly."""
        if p.pending_resp is sess:
            p.pending_resp = None
        old = p.current
        if old is not None and old is not sess:
            if p.previous is not None and p.previous is not old:
                self._drop(p.previous)
            old.retired = now
            p.previous = old
        p.current = sess
        p.unknown = 0
        p.established += 1
        self.stats['sessions'] += 1
        out.append(('log', 'secure session with %d established (%s, '
                           'peer key %s)%s'
                    % (p.addr, 'rekey' if old else 'new',
                       self.fingerprint(p.addr),
                       '' if C.BACKEND == 'cryptography'
                       else ' [pure-python crypto]')))
        self._state_evt(out, p, 'secure', fp=self.fingerprint(p.addr))
        out.append(('evt', dict(e='sec_session', peer=p.addr,
                                rekey=old is not None)))

    # --------------------------------------------------------- handshake
    def _start_init(self, out, p, now):
        rs = self.keys.peer_public(p.addr)
        if rs is None:
            self._state_evt(out, p, 'no-key')
            p.next_try = now + HS_RETRY_MAX
            return
        idx = self._new_index()
        ts = self.keys.next_hs_timestamp()
        payload = struct.pack('!QII', ts, idx, self.boot_id)
        prolog = prologue(self.keys.network, self.my_addr, p.addr)
        msg, st = kk_initiator_start(prolog, self.keys.private,
                                     self.keys.public, rs, payload)
        p.init = {'idx': idx, 'state': st, 'sent': now}
        p.tries += 1
        p.unanswered += 1
        p.next_try = now + self.retry_interval(p)
        self.stats['hs_tx'] += 1
        if p.current is None:
            self._state_evt(out, p, 'handshaking')
        body = bytes([SEC_VERSION, self.my_addr, p.addr]) + msg
        out.append(('tx', L.build(FT_HS_INIT, p.addr, self.my_addr, 0, 0,
                                  body, 0, idx & 0xFF)))

    def resp_holdoff(self):
        """How long an answered handshake waits for its confirmation before
        this node initiates one of its own: RESP_HOLDOFF, or 1.5 round trips
        plus 0.5 s on a slow link."""
        if self.rtt is None:
            return RESP_HOLDOFF
        return max(RESP_HOLDOFF, 1.5 * self.rtt + 0.5)

    def retry_interval(self, p):
        """Wait before the next handshake attempt (r6.1: tuned to the link).

        Base: 2 x the measured round trip + 0.3 s, at least 0.5 s (HS_RETRY
        until a round trip has been measured); doubled per unanswered
        attempt up to HS_RETRY_MAX - or up to the base itself when the round
        trip is longer than that: a retry replaces the attempt in flight, so
        retrying faster than an answer can arrive never completes (r6.0.1 on
        a host with a 4.6 s echo: 'securing link...' for ever). A peer that
        does not hear this node at all (the application sets p.slow) is
        retried only at the cap - its answer cannot come."""
        if self.rtt is None:
            base, cap = HS_RETRY, HS_RETRY_MAX
        else:
            base = max(0.5, 2.0 * self.rtt + 0.3)
            cap = max(HS_RETRY_MAX, base)
        if p.slow:
            return cap
        return min(cap, base * (2 ** min(max(p.tries, 1) - 1, 3)))

    def _on_init(self, out, frm, now):
        pl = frm['payload']
        if len(pl) < 3 + 32 + C.TAG_LEN or pl[0] != SEC_VERSION:
            self.stats['hs_fail'] += 1
            return
        origin, final = pl[1], pl[2]
        rs = self.keys.peer_public(origin)
        if final != self.my_addr or rs is None:
            self.stats['hs_fail'] += 1
            if rs is None and final == self.my_addr:
                last = self._last_refused.get(origin, -1e9)
                if now - last > 10.0:
                    self._last_refused[origin] = now
                    out.append(('log', 'handshake from %d refused: %s'
                                % (origin, self._why_untrusted(origin))))
                    out.append(('evt', dict(e='sec_untrusted', peer=origin)))
            return
        p = self._peer(origin)
        my_idx = self._new_index()
        seen = {}

        def respond(init_payload):
            if len(init_payload) != 16:
                raise C.AuthError('bad initiation payload')
            ts, their_idx, boot = struct.unpack('!QII', init_payload)
            if ts <= max(p.last_init_ts, self._init_ts.get(rs, 0)):
                raise C.AuthError('stale or replayed initiation')
            seen.update(ts=ts, idx=their_idx, boot=boot)
            return struct.pack('!II', my_idx, self.boot_id)
        try:
            _, response, k_send, k_recv, hsh = kk_responder(
                prologue(self.keys.network, origin, self.my_addr),
                self.keys.private, self.keys.public, rs, pl[3:], respond)
        except (C.AuthError, ValueError):
            self.stats['hs_fail'] += 1
            return
        self.stats['hs_rx'] += 1
        p.last_init_ts = seen['ts']
        self._init_ts[rs] = seen['ts']
        self._check_boot(out, p, seen['boot'])
        if p.init is not None and self.my_addr > origin:
            # Both ends initiated at once: the lower address wins, so this
            # (higher) node abandons its own attempt and answers instead.
            p.init = None
        sess = Session(origin, 'resp', my_idx, seen['idx'], k_send, k_recv,
                       now, False, hsh)
        self._drop(p.pending_resp)
        p.pending_resp = sess
        self.by_idx[my_idx] = sess
        body = (bytes([SEC_VERSION, self.my_addr, origin])
                + struct.pack('!I', seen['idx']) + response)
        self.stats['hs_tx'] += 1
        out.append(('tx', L.build(FT_HS_RESP, origin, self.my_addr, 0, 0,
                                  body, 0, my_idx & 0xFF)))

    def _on_resp(self, out, frm, now):
        pl = frm['payload']
        if len(pl) < 7 + 32 + C.TAG_LEN or pl[0] != SEC_VERSION:
            self.stats['hs_fail'] += 1
            return
        origin, final = pl[1], pl[2]
        echo = struct.unpack('!I', pl[3:7])[0]
        p = self.peers.get(origin)
        if final != self.my_addr or p is None:
            self.stats['hs_fail'] += 1
            return
        if p.init is None or p.init['idx'] != echo:
            # Answer to an attempt we already replaced or abandoned (a retry,
            # or the simultaneous-initiation tie-break). Expected, harmless.
            self.stats['hs_stale'] += 1
            return
        try:
            payload, k_send, k_recv, hsh = kk_initiator_finish(
                p.init['state'], pl[7:])
            if len(payload) != 8:
                raise C.AuthError('bad response payload')
        except (C.AuthError, ValueError):
            self.stats['hs_fail'] += 1
            return
        their_idx, boot = struct.unpack('!II', payload)
        my_idx = p.init['idx']
        p.init = None
        p.tries = 0
        p.unanswered = 0
        self.stats['hs_rx'] += 1
        self._check_boot(out, p, boot)
        sess = Session(origin, 'init', my_idx, their_idx, k_send, k_recv,
                       now, True, hsh)
        self.by_idx[my_idx] = sess
        self._promote(out, p, sess, now)
        p.next_try = now + REKEY_AFTER
        # Confirm at once with a sealed keep-alive so the responder can start
        # transmitting on the new session without waiting for our traffic.
        keep = L.build(FT_IDLE, origin, self.my_addr, 0, 0, b'', 0, 0)
        sealed = self.protect(keep, now)
        if sealed is not None:
            out.append(('tx', sealed))

    def _why_untrusted(self, origin):
        binding = getattr(self.keys, 'binding', None)
        if binding is None:
            return 'its address is not in roster.json'
        pub = binding(origin)
        if pub is None:
            return 'no beacon heard from that address yet'
        return ('its key %s has not been approved on this node'
                % fingerprint(pub))

    def _check_boot(self, out, p, boot):
        if p.boot_id is not None and boot != p.boot_id:
            out.append(('log', 'peer %d restarted (new boot id)' % p.addr))
            out.append(('evt', dict(e='peer_restarted', peer=p.addr)))
            # Sessions from its previous life are useless now.
            for s in (p.current, p.previous):
                self._drop(s)
            p.current = p.previous = None
            if p.state == 'secure':
                self._state_evt(out, p, 'handshaking')
        p.boot_id = boot

    # --------------------------------------------------------------- tick
    def on_tick(self, now):
        out = []
        for p in self.peers.values():
            cur = p.current
            if cur is not None and (now - cur.created) >= REJECT_AFTER:
                self._drop(cur)
                p.current = None
                out.append(('log', 'secure session with %d expired' % p.addr))
                self._state_evt(out, p, 'handshaking')
            if (p.previous is not None and p.previous.retired is not None
                    and now - p.previous.retired > PREV_GRACE):
                self._drop(p.previous)
                p.previous = None
            if (p.pending_resp is not None
                    and now - p.pending_resp.created > max(
                        PENDING_TTL, 3.0 * (self.rtt or 0.0))):
                self._drop(p.pending_resp)
                p.pending_resp = None
            cur = p.current
            # Rekey from the side that initiated; the responder steps in only
            # if the initiator has fallen well behind.
            due = (cur is None
                   or (cur.role == 'init' and now - cur.created > REKEY_AFTER)
                   or now - cur.created > REKEY_AFTER + 120.0)
            # r6.1: the peer keeps transmitting on a session this node does
            # not hold while ours is current - the two ends settled on
            # different sessions (handshakes crossed on a slow link: each
            # side's current session paired with the other's 'previous',
            # which expires after PREV_GRACE; r6.0.1 then lost every frame
            # until the 10-minute rekey). Start over; the lower address wins
            # a crossing, so this converges.
            if cur is not None and p.unknown >= RESYNC_UNKNOWN:
                due = True
            # We just answered the peer's handshake and are waiting for its
            # confirmation: starting one of our own now would only produce a
            # second, redundant session. Wait a moment (r6.1: at least the
            # measured round trip, the confirmation cannot come sooner); if
            # the confirmation was lost, initiate after all.
            answering = (p.pending_resp is not None
                         and now - p.pending_resp.created < self.resp_holdoff())
            if due and not answering and not p.paused and now >= p.next_try:
                self._start_init(out, p, now)
        return out

    # ---------------------------------------------------------- telemetry
    def snapshot(self, now, addr):
        p = self.peers.get(addr) if addr is not None else None
        cur = p.current if p else None
        return {'enabled': True, 'strict': self.strict, 'backend': C.BACKEND,
                'suite': 'Noise_KK_25519_ChaChaPoly_SHA256',
                'state': p.state if p else 'idle',
                'my_fp': self.fingerprint(),
                'peer_fp': self.fingerprint(addr) if addr is not None else None,
                'peer_in_roster': (addr is not None
                                   and self.keys.peer_public(addr) is not None),
                'session_age': (now - cur.created) if cur else None,
                'rekey_in': (max(0.0, REKEY_AFTER - (now - cur.created))
                             if cur is not None and cur.role == 'init'
                             else None),
                'sessions': p.established if p else 0,
                'unanswered': p.unanswered if p else 0,
                'rtt': self.rtt,
                'network': self.keys.network,
                'stats': dict(self.stats)}
