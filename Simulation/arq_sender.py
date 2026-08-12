import socket
import time

TX_PORT = 52001  # Sends packets TO GRC
RX_PORT = 52002  # Receives ACKs FROM GRC

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", RX_PORT))

def clear_buffer(s):
    """ Instantly flushes stale packets from the socket """
    s.setblocking(False)
    try:
        while True:
            s.recv(1024)
    except (BlockingIOError, socket.error):
        pass
    s.setblocking(True)

def wait_for_ack(s, timeout=3.5):
    """ Listens continuously until a valid ACK arrives or timeout expires """
    start_time = time.time()
    s.settimeout(0.2)  # Short polling window
    
    while (time.time() - start_time) < timeout:
        try:
            data, _ = s.recvfrom(1024)
            msg = data.decode('utf-8', errors='ignore').strip()
            if "ACK" in msg:
                return True
        except socket.timeout:
            continue
        except Exception:
            pass
    return False

packets = [
    "First Packet",
    "Second Packet",
    "Hello from Terminal 1",
    "SDR Packet Radios are working!",
    "Hello",
    "Final Test Packet"
]

print("=== Robust Stop-and-Wait ARQ Sender ===\n")

for i, msg in enumerate(packets, 1):
    success = False
    retries = 0
    max_retries = 3

    while not success and retries < max_retries:
        retries += 1
        
        # Flush stale socket buffer before sending
        clear_buffer(sock)
        
        print(f"[{i}/{len(packets)}] Transmitting (Attempt {retries}): '{msg}'")
        sock.sendto(msg.encode('utf-8'), ("127.0.0.1", TX_PORT))
        
        # Wait up to 3.5 seconds for a valid ACK response
        if wait_for_ack(sock, timeout=3.5):
            print(f"    --> [SUCCESS] ACK Received!\n")
            success = True
        else:
            print(f"    --> [TIMEOUT] No valid ACK received.")
            # Critical: Give GNU Radio modem 1.0s guard time to recover phase lock
            time.sleep(1.0)

    if not success:
        print(f"    --> [FAILED] Dropped '{msg}' after {max_retries} attempts.\n")
        time.sleep(1.0)

    time.sleep(0.5)  # Pace transmissions cleanly

print("=== Transmission Complete ===")
