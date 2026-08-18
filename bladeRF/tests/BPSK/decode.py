data = open('rx_output.bin', 'rb').read()
text = ''.join(chr(b) for b in data if 32 <= b < 127)
print(text)
