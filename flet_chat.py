import flet as ft
import socket
import threading
import sys
from cryptography.fernet import Fernet

# --- Encryption & Network Setup ---
SHARED_KEY = b'wX4yP3D7m8T9Qj1VzY6rNkFaLcH0gB5sE2vM_qW4UoE='
cipher = Fernet(SHARED_KEY)

TX_PORT = 52001
RX_PORT = 52002
IP_ADDRESS = "127.0.0.1"

# Bind the receiver socket
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((IP_ADDRESS, RX_PORT))
except Exception as e:
    print(f"Port error: {e}")
    sys.exit(1)


def main(page: ft.Page):
    # --- Page Configuration ---
    page.title = "SDR Secure Chat"
    page.theme_mode = ft.ThemeMode.DARK
    page.window.width = 500
    page.window.height = 650
    page.padding = 20

    # UI Elements
    chat_list = ft.ListView(expand=True, spacing=10, auto_scroll=True)

    def add_message(text, bg_color, text_color, align=ft.MainAxisAlignment.START):
        """Helper to format and add bubble messages to the screen"""
        chat_list.controls.append(
            ft.Row(
                [
                    # Wrap the text in a Container to create a "bubble"
                    ft.Container(
                        content=ft.Text(text, color=text_color, size=15),
                        bgcolor=bg_color,
                        padding=12,
                        border_radius=15,
                        # FIX: max_width was removed as it is not supported in Flet Containers
                    )
                ],
                alignment=align,
            )
        )
        page.update()

    def receive_messages():
        """Background listener for SDR UDP packets"""
        while True:
            try:
                data, _ = sock.recvfrom(1024)
                try:
                    decrypted = cipher.decrypt(data).decode('utf-8')
                    # Incoming messages: Green bubble on the left
                    add_message(f"Node 1: {decrypted}", ft.Colors.GREEN_800, ft.Colors.WHITE)
                except:
                    raw = data.decode('utf-8', errors='ignore').strip()
                    if "ACK" in raw:
                        # System messages: Small grey bubble in the center
                        add_message("✓ ACK Received", ft.Colors.GREY_800, ft.Colors.WHITE, ft.MainAxisAlignment.CENTER)
                    else:
                        add_message("[!] Garbled frame", ft.Colors.RED_800, ft.Colors.WHITE, ft.MainAxisAlignment.CENTER)
            except Exception as e:
                pass

    def send_message(e):
        """Triggered on button click or Enter key"""
        msg = message_entry.value.strip()
        if msg:
            enc = cipher.encrypt(msg.encode('utf-8'))
            
            if len(enc) > 128:
                add_message("Error: Message too long!", ft.Colors.RED_800, ft.Colors.WHITE, ft.MainAxisAlignment.CENTER)
                return
                
            sock.sendto(enc, (IP_ADDRESS, TX_PORT))
            
            # Outgoing messages: Blue bubble on the right
            add_message(f"{msg}", ft.Colors.BLUE_700, ft.Colors.WHITE, ft.MainAxisAlignment.END)
            
            # Clear input box
            message_entry.value = ""
            page.update()

    # Input Fields
    message_entry = ft.TextField(
        hint_text="Type a secure message...", 
        expand=True, 
        on_submit=send_message,
        border_radius=20
    )
    
    send_button = ft.ElevatedButton(
        "Send", 
        on_click=send_message, 
        bgcolor=ft.Colors.BLUE_700, 
        color=ft.Colors.WHITE,
        height=50
    )

    # Build the screen layout
    page.add(
        chat_list,
        ft.Row([message_entry, send_button], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    )

    # Start network listener thread
    threading.Thread(target=receive_messages, daemon=True).start()

# Launch the app
ft.app(target=main)
