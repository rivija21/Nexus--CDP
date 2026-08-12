import socket
import threading
import tkinter as tk
from tkinter import scrolledtext

# --- Network Configuration ---
TX_PORT = 52001  # Port to send payloads TO GNU Radio
RX_PORT = 52002  # Port to listen for incoming messages FROM GNU Radio
IP_ADDRESS = "127.0.0.1"

# Initialize the UDP Socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((IP_ADDRESS, RX_PORT))

def receive_messages():
    """Background thread to continuously listen for incoming UDP packets."""
    while True:
        try:
            # Wait for data from the SDR flowgraph
            data, _ = sock.recvfrom(1024)
            message = data.decode('utf-8', errors='ignore').strip()
            
            if message:
                # Update the chat area safely
                chat_area.config(state=tk.NORMAL)
                chat_area.insert(tk.END, f"Received: {message}\n", "received")
                chat_area.config(state=tk.DISABLED)
                chat_area.yview(tk.END) # Auto-scroll to bottom
        except Exception as e:
            print(f"Receiver error: {e}")

def send_message(event=None):
    """Triggered when the user clicks Send or hits Enter."""
    msg = message_entry.get().strip()
    if msg:
        # 1. Send the string over UDP to the GNU Radio flowgraph
        sock.sendto(msg.encode('utf-8'), (IP_ADDRESS, TX_PORT))
        
        # 2. Display the sent message in our local chat window
        chat_area.config(state=tk.NORMAL)
        chat_area.insert(tk.END, f"Me: {msg}\n", "sent")
        chat_area.config(state=tk.DISABLED)
        chat_area.yview(tk.END)
        
        # 3. Clear the text input box
        message_entry.delete(0, tk.END)

# --- GUI Setup (tkinter) ---
root = tk.Tk()
root.title("SDR Two-Way Chat Interface")
root.geometry("450x400")

# Chat History Area
chat_area = scrolledtext.ScrolledText(root, state=tk.DISABLED, wrap=tk.WORD, font=("Arial", 10))
chat_area.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

# Define text colors for "Me" vs "Received"
chat_area.tag_config("sent", foreground="blue")
chat_area.tag_config("received", foreground="green")

# Input Frame (Entry + Button)
input_frame = tk.Frame(root)
input_frame.pack(padx=10, pady=(0, 10), fill=tk.X)

# Text Entry Box
message_entry = tk.Entry(input_frame, font=("Arial", 12))
message_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
# Bind the "Enter" key to trigger the send_message function
message_entry.bind("<Return>", send_message) 

# Send Button
send_button = tk.Button(input_frame, text="Send", font=("Arial", 10, "bold"), bg="#4CAF50", fg="white", command=send_message)
send_button.pack(side=tk.RIGHT, padx=(10, 0), ipadx=10, ipady=3)

# --- Start the Application ---
# Start the background receiver thread as a 'daemon' (closes automatically when GUI closes)
listen_thread = threading.Thread(target=receive_messages, daemon=True)
listen_thread.start()

# Start the GUI loop
