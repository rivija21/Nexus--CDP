"""
Decoder for the robust BPSK frame produced by msg_BPSK_tx.grc /
msg_BPSK_rx.grc.

Frame layout (as transmitted, before differential precoding):
    4 bytes   preamble    0xAA 0xAA 0xAA 0xAA   (AGC / symbol-timing settle)
    2 bytes   access code 0x2D 0xD4              (must match the RX
                                                    correlate_access_code block)
    1 byte    length      L = number of payload bytes (0-255)
    L bytes   payload     the text message
    4 bytes   CRC32       big-endian, over (length_byte + payload)

The RX flowgraph now differentially decodes the bitstream before the
access-code correlator, so phase ambiguity from the Costas loop is
already resolved in the .grc itself -- we no longer need to guess by
searching an inverted copy of the bitstream. It's kept below anyway as
a defensive fallback in case the diff-decoder block is bypassed/absent.

Because the TX repeats the frame continuously, rx_output.bin generally
contains many back-to-back (and not necessarily byte-aligned) copies.
This script scans the *bit* stream for every occurrence of the access
code, and for each hit parses the length + payload + CRC32 and only
accepts frames whose CRC32 matches -- corrupted repetitions (bit errors
from noise, a partial/clipped capture, etc.) are reported and dropped
instead of being printed as garbage.
"""

import zlib

SYNC_BITS = '0010110111010100'  # 0x2D, 0xD4


def bytes_to_bitstring(data: bytes) -> str:
    return ''.join(f'{b:08b}' for b in data)


def bits_to_bytes(bits: str) -> bytes:
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


def find_frames(bitstring: str):
    """Yield (bit_offset, length, payload_bytes, crc_ok) for every
    sync-word occurrence in bitstring that has enough trailing bits to
    contain a full length+payload+CRC32 field."""
    n = len(bitstring)
    search_from = 0
    while True:
        idx = bitstring.find(SYNC_BITS, search_from)
        if idx == -1:
            return
        start = idx + len(SYNC_BITS)
        if start + 8 > n:
            return
        length = int(bitstring[start:start + 8], 2)
        payload_start = start + 8
        payload_end = payload_start + length * 8
        crc_end = payload_end + 32
        if crc_end > n:
            # Not enough bits left for a full frame at this hit; keep
            # scanning in case an earlier/later hit is real.
            search_from = idx + 1
            continue

        payload = bits_to_bytes(bitstring[payload_start:payload_end])
        crc_recv = int(bitstring[payload_end:crc_end], 2).to_bytes(4, 'big')
        crc_calc = zlib.crc32(bytes([length]) + payload).to_bytes(4, 'big')

        yield (idx, length, payload, crc_recv == crc_calc)
        search_from = idx + 1


def decode_stream(bitstring: str, label: str):
    frames = list(find_frames(bitstring))
    if not frames:
        return []

    print(f"=== {label} ===")
    valid = []
    for idx, length, payload, ok in frames:
        text = payload.decode('utf-8', errors='replace')
        status = 'VALID' if ok else 'CORRUPT (CRC mismatch, dropped)'
        print(f"  bit_offset={idx:6d}  len={length:3d}  {status:32s} {text!r}")
        if ok:
            valid.append(payload)
    return valid


def main():
    data = open('rx_output.bin', 'rb').read()
    bits = bytes_to_bitstring(data)
    bits_inv = ''.join('1' if c == '0' else '0' for c in bits)

    valid = decode_stream(bits, "Normal bitstream")
    if not valid:
        print("No valid (CRC-passing) frames in the normal bitstream; "
              "checking bit-inverted copy as a fallback...")
        valid = decode_stream(bits_inv, "Bit-inverted bitstream (fallback)")

    if not valid:
        print("Could not find any CRC-valid frame in either orientation.")
        return

    unique = sorted(set(valid), key=valid.index)
    print()
    print(f"{len(valid)} valid repetition(s) received, "
          f"{len(unique)} unique message(s):")
    for payload in unique:
        print(" ->", payload.decode('utf-8', errors='replace'))


if __name__ == '__main__':
    main()
