import socket
import threading
import tkinter as tk
from tkinter import scrolledtext
import sys

print("1. Starting script...")

# --- Network Configuration ---
TX_PORT = 52001
RX_PORT = 52002
IP_ADDRESS = "127.0.0.1"

# Initialize the UDP Socket
print("2. Binding network ports...")
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((IP_ADDRESS, RX_PORT))
except Exception as e:
    print(f"CRITICAL ERROR: Could not bind to port {RX_PORT}. Is it in use?")
    print(e)
    sys.exit(1)

def update_gui_with_message(message):
    """This runs safely on the MAIN thread to update the UI."""
    chat_area.config(state=tk.NORMAL)
    chat_area.insert(tk.END, f"Received: {message}\n", "received")
    chat_area.config(state=tk.DISABLED)
    chat_area.yview(tk.END)

def receive_messages():
    """Background thread to continuously listen for incoming UDP packets."""
    print("   -> Receiver thread started and listening...")
    while True:
        try:
            # Wait for data from the SDR flowgraph
            data, _ = sock.recvfrom(1024)
            message = data.decode('utf-8', errors='ignore').strip()
            
            if message:
                # DANGER: Do not touch the GUI here!
                # Safely schedule the update on the main thread using root.after()
                root.after(0, update_gui_with_message, message)
                
        except Exception as e:
            print(f"Receiver error: {e}")

def send_message(event=None):
    """Triggered when the user clicks Send or hits Enter."""
    msg = message_entry.get().strip()
    if msg:
        # 1. Send the string over UDP
        sock.sendto(msg.encode('utf-8'), (IP_ADDRESS, TX_PORT))
        
        # 2. Display the sent message locally
        chat_area.config(state=tk.NORMAL)
        chat_area.insert(tk.END, f"Me: {msg}\n", "sent")
        chat_area.config(state=tk.DISABLED)
        chat_area.yview(tk.END)
        
        # 3. Clear the text input box
        message_entry.delete(0, tk.END)

# --- GUI Setup (tkinter) ---
print("3. Building the GUI...")
root = tk.Tk()
root.title("SDR Two-Way Chat Interface")
root.geometry("450x400")

# Chat History Area
chat_area = scrolledtext.ScrolledText(root, state=tk.DISABLED, wrap=tk.WORD, font=("Arial", 10))
chat_area.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

chat_area.tag_config("sent", foreground="blue")
chat_area.tag_config("received", foreground="green")

# Input Frame (Entry + Button)
input_frame = tk.Frame(root)
input_frame.pack(padx=10, pady=(0, 10), fill=tk.X)

message_entry = tk.Entry(input_frame, font=("Arial", 12))
message_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
message_entry.bind("<Return>", send_message) 

send_button = tk.Button(input_frame, text="Send", font=("Arial", 10, "bold"), bg="#4CAF50", fg="white", command=send_message)
send_button.pack(side=tk.RIGHT, padx=(10, 0), ipadx=10, ipady=3)

# --- Start the Application ---
print("4. Launching background receiver thread...")
listen_thread = threading.Thread(target=receive_messages, daemon=True)
listen_thread.start()

print("5. Opening window (if you don't see it, check behind other windows!)")
root.mainloop()
