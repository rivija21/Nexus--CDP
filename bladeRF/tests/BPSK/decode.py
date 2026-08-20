data = open('rx_output.bin', 'rb').read()

# Convert entire byte array to MSB-first bit string
bits = ''.join(f'{b:08b}' for b in data)
bits_inv = ''.join('1' if c == '0' else '0' for c in bits)

# Sync word: 0x2D (00101101), 0xD4 (11010100)
sync_bits = '0010110111010100'

def extract_payload(bit_stream, sync_pattern, label):
    pos = bit_stream.find(sync_pattern)
    if pos != -1:
        print(f"=== {label} ===")
        print(f"Found sync pattern at bit index: {pos} (Bit Shift: {pos % 8})")
        
        # Start reading right after the sync word
        start_bit = pos + len(sync_pattern)
        
        # Extract 12 bytes (96 bits) for "Hello World!"
        payload_bits = bit_stream[start_bit : start_bit + (12 * 8)]
        
        # Pack bits back into bytes
        payload_bytes = bytes(
            int(payload_bits[i:i+8], 2) for i in range(0, len(payload_bits), 8)
        )
        
        print("Decoded Payload:", payload_bytes.decode('latin1', errors='ignore'))
        return True
    return False

# Search normal and inverted bitstreams
found = extract_payload(bits, sync_bits, "Found Normal Sync (Bit Aligned)") or \
        extract_payload(bits_inv, sync_bits, "Found Inverted Sync (Phase Flip + Bit Aligned)")

if not found:
    print("Sync pattern not found in bitstream. Checking reverse bit-endianness...")
    # LSB-first sync word fallback (0x2D -> 10110100, 0xD4 -> 00101011)
    sync_bits_lsb = '1011010000101011'
    if not (extract_payload(bits, sync_bits_lsb, "Found LSB Normal Sync") or
            extract_payload(bits_inv, sync_bits_lsb, "Found LSB Inverted Sync")):
        print("Could not find sync pattern in any orientation.")
