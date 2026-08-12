import socket
import time

UDP_IP = "127.0.0.1"
UDP_PORT = 52001

messages = [
    "First Packet",
    "Second Packet",
    "Hello from Terminal 1",
    "SDR Packet Radios are working!",
    "Hello",
    "Final Test Packet"
]

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print("--- Starting Paced Transmission Test (1.5s delay between packets) ---\n")

for i, msg in enumerate(messages, 1):
    print(f"[{i}/{len(messages)}] Transmitting: \"{msg}\"...")
    sock.sendto(msg.encode('utf-8'), (UDP_IP, UDP_PORT))
    
    # Crucial: Give the modem time to modulate, transmit, receive, 
    # generate the ACK, and clear the DSP buffer before sending the next frame.
    time.sleep(1.5)

print("\n--- Transmission Complete ---")
