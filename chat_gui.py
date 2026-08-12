import socket
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk
from datetime import datetime
import sys

print("1. Starting script...")

# --- Network Configuration ---
TX_PORT = 52001
RX_PORT = 52002
IP_ADDRESS = "127.0.0.1"

# --- Color Palette ---
BG_DARK = "#1e1f26"        # window background
BG_PANEL = "#262832"       # chat area background
BG_INPUT = "#2f3140"       # entry field background
ACCENT = "#5b8cff"         # primary accent (buttons, sent bubbles)
ACCENT_HOVER = "#4a76e0"
RECEIVED_COLOR = "#3ecf8e" # incoming message color
SENT_COLOR = "#5b8cff"     # outgoing message color
TEXT_LIGHT = "#e8e9ee"
TEXT_MUTED = "#8b8d9a"
STATUS_ONLINE = "#3ecf8e"

# Initialize the UDP Socket
print("2. Binding network ports...")
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((IP_ADDRESS, RX_PORT))
except Exception as e:
    print(f"CRITICAL ERROR: Could not bind to port {RX_PORT}. Is it in use?")
    print(e)
    sys.exit(1)


def timestamp():
    return datetime.now().strftime("%H:%M:%S")


def update_gui_with_message(message):
    """This runs safely on the MAIN thread to update the UI."""
    chat_area.config(state=tk.NORMAL)
    chat_area.insert(tk.END, f"\n[{timestamp()}] ", "meta")
    chat_area.insert(tk.END, "Peer\n", "received_label")
    chat_area.insert(tk.END, f"{message}\n", "received")
    chat_area.config(state=tk.DISABLED)
    chat_area.yview(tk.END)
    flash_status()


def receive_messages():
    """Background thread to continuously listen for incoming UDP packets."""
    print("   -> Receiver thread started and listening...")
    while True:
        try:
            data, _ = sock.recvfrom(1024)
            message = data.decode('utf-8', errors='ignore').strip()
            if message:
                # DANGER: Do not touch the GUI here!
                root.after(0, update_gui_with_message, message)
        except Exception as e:
            print(f"Receiver error: {e}")


def flash_status():
    """Briefly flash the status dot when a message arrives."""
    status_dot.config(fg="#ffd166")
    root.after(250, lambda: status_dot.config(fg=STATUS_ONLINE))


def send_message(event=None):
    """Triggered when the user clicks Send or hits Enter."""
    msg = message_entry.get().strip()
    if msg:
        sock.sendto(msg.encode('utf-8'), (IP_ADDRESS, TX_PORT))

        chat_area.config(state=tk.NORMAL)
        chat_area.insert(tk.END, f"\n[{timestamp()}] ", "meta")
        chat_area.insert(tk.END, "Me\n", "sent_label")
        chat_area.insert(tk.END, f"{msg}\n", "sent")
        chat_area.config(state=tk.DISABLED)
        chat_area.yview(tk.END)

        message_entry.delete(0, tk.END)


def on_entry_focus_in(event):
    if message_entry.get() == placeholder_text:
        message_entry.delete(0, tk.END)
        message_entry.config(fg=TEXT_LIGHT)


def on_entry_focus_out(event):
    if not message_entry.get():
        message_entry.insert(0, placeholder_text)
        message_entry.config(fg=TEXT_MUTED)


# --- GUI Setup (tkinter) ---
print("3. Building the GUI...")
root = tk.Tk()
root.title("SDR Two-Way Chat Interface")
root.geometry("480x560")
root.configure(bg=BG_DARK)
root.minsize(380, 420)

# ---- Header ----
header = tk.Frame(root, bg=BG_DARK)
header.pack(fill=tk.X, padx=16, pady=(16, 8))

title_label = tk.Label(
    header, text="📡 SDR Chat", font=("Segoe UI", 16, "bold"),
    bg=BG_DARK, fg=TEXT_LIGHT
)
title_label.pack(side=tk.LEFT)

status_frame = tk.Frame(header, bg=BG_DARK)
status_frame.pack(side=tk.RIGHT)

status_dot = tk.Label(status_frame, text="●", font=("Segoe UI", 12), bg=BG_DARK, fg=STATUS_ONLINE)
status_dot.pack(side=tk.LEFT, padx=(0, 4))

status_label = tk.Label(
    status_frame, text=f"Listening on :{RX_PORT}", font=("Segoe UI", 9),
    bg=BG_DARK, fg=TEXT_MUTED
)
status_label.pack(side=tk.LEFT)

subtitle = tk.Label(
    root, text=f"Sending to {IP_ADDRESS}:{TX_PORT}", font=("Segoe UI", 9),
    bg=BG_DARK, fg=TEXT_MUTED, anchor="w"
)
subtitle.pack(fill=tk.X, padx=16)

# ---- Chat History Area ----
chat_container = tk.Frame(root, bg=BG_PANEL, highlightbackground="#3a3c4a", highlightthickness=1)
chat_container.pack(padx=16, pady=12, fill=tk.BOTH, expand=True)

chat_area = scrolledtext.ScrolledText(
    chat_container, state=tk.DISABLED, wrap=tk.WORD,
    font=("Segoe UI", 10), bg=BG_PANEL, fg=TEXT_LIGHT,
    borderwidth=0, highlightthickness=0, padx=12, pady=10,
    insertbackground=TEXT_LIGHT
)
chat_area.pack(fill=tk.BOTH, expand=True)

chat_area.tag_config("meta", foreground=TEXT_MUTED, font=("Segoe UI", 8))
chat_area.tag_config("sent_label", foreground=SENT_COLOR, font=("Segoe UI", 9, "bold"))
chat_area.tag_config("received_label", foreground=RECEIVED_COLOR, font=("Segoe UI", 9, "bold"))
chat_area.tag_config("sent", foreground=TEXT_LIGHT, lmargin1=10, lmargin2=10, spacing3=4)
chat_area.tag_config("received", foreground=TEXT_LIGHT, lmargin1=10, lmargin2=10, spacing3=4)

# Welcome message
chat_area.config(state=tk.NORMAL)
chat_area.insert(tk.END, "Chat started — waiting for messages...\n", "meta")
chat_area.config(state=tk.DISABLED)

# ---- Input Frame (Entry + Button) ----
input_frame = tk.Frame(root, bg=BG_DARK)
input_frame.pack(padx=16, pady=(0, 16), fill=tk.X)

entry_wrapper = tk.Frame(input_frame, bg=BG_INPUT, highlightbackground="#3a3c4a", highlightthickness=1)
entry_wrapper.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)

placeholder_text = "Type a message..."
message_entry = tk.Entry(
    entry_wrapper, font=("Segoe UI", 11), bg=BG_INPUT, fg=TEXT_MUTED,
    borderwidth=0, highlightthickness=0, insertbackground=TEXT_LIGHT
)
message_entry.pack(fill=tk.X, expand=True, padx=10)
message_entry.insert(0, placeholder_text)
message_entry.bind("<FocusIn>", on_entry_focus_in)
message_entry.bind("<FocusOut>", on_entry_focus_out)
message_entry.bind("<Return>", send_message)


def on_button_enter(e):
    send_button.config(bg=ACCENT_HOVER)


def on_button_leave(e):
    send_button.config(bg=ACCENT)


send_button = tk.Button(
    input_frame, text="Send ➤", font=("Segoe UI", 10, "bold"),
    bg=ACCENT, fg="white", activebackground=ACCENT_HOVER, activeforeground="white",
    borderwidth=0, cursor="hand2", command=send_message
)
send_button.pack(side=tk.RIGHT, padx=(10, 0), ipadx=14, ipady=8)
send_button.bind("<Enter>", on_button_enter)
send_button.bind("<Leave>", on_button_leave)

# --- Start the Application ---
print("4. Launching background receiver thread...")
listen_thread = threading.Thread(target=receive_messages, daemon=True)
listen_thread.start()

print("5. Opening window (if you don't see it, check behind other windows!)")
root.mainloop()
