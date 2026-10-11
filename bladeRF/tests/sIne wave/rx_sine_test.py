import SoapySDR
from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
import numpy as np
import matplotlib.pyplot as plt
import sys

# 1. Configuration Parameters
freq = 2450e6      # Center frequency: 2.45 GHz
rate = 1e6         # Sample rate: 1 MSps
rx_gain = 40       # RX Gain in dB (Adjust based on distance)
buffer_size = 8192 # Larger buffer yields higher frequency resolution in FFT

# 2. Hardware Initialization
print("Initializing bladeRF receiver...")
args = dict(driver="bladerf")
try:
    sdr = SoapySDR.Device(args)
except Exception as e:
    print(f"Error initializing device: {e}")
    sys.exit(1)

# 3. Channel Setup (RX Channel 0)
sdr.setSampleRate(SOAPY_SDR_RX, 0, rate)
sdr.setFrequency(SOAPY_SDR_RX, 0, freq)
sdr.setGain(SOAPY_SDR_RX, 0, rx_gain)
sdr.setAntenna(SOAPY_SDR_RX, 0, "RX")

rx_stream = sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
sdr.activateStream(rx_stream)

print("Hardware configured. Capturing samples...")

# 4. Flush stale buffers to allow Local Oscillator (LO) to settle
dummy_buff = np.zeros(buffer_size, np.complex64)
for _ in range(10):
    sdr.readStream(rx_stream, [dummy_buff], buffer_size)

# 5. Capture the active buffer
rx_buff = np.zeros(buffer_size, np.complex64)
status = sdr.readStream(rx_stream, [rx_buff], buffer_size)

# 6. Teardown
sdr.deactivateStream(rx_stream)
sdr.closeStream(rx_stream)

if status.ret != buffer_size:
    print(f"Failed to read requested samples. Read: {status.ret}")
    sys.exit(1)

# 7. Signal Processing (FFT computation)
print("Computing FFT...")
# Shift zero-frequency component to the center of the spectrum
fft_result = np.fft.fftshift(np.fft.fft(rx_buff))

# Calculate magnitude in dB, adding a small offset to prevent log(0) errors
fft_mag_db = 20 * np.log10(np.abs(fft_result) + 1e-12) 

# Generate frequency axis centered at 0 Hz (Baseband)
freqs = np.fft.fftshift(np.fft.fftfreq(buffer_size, 1/rate))

# 8. Visualization
plt.figure(figsize=(10, 6))
plt.plot(freqs / 1e3, fft_mag_db, color='blue') # Plot X-axis in kHz
plt.title(f"Received Spectrum (Center: {freq / 1e6} MHz)")
plt.xlabel("Frequency Offset (kHz)")
plt.ylabel("Magnitude (dB)")
plt.grid(True)
plt.xlim([-500, 500])

# Mark the exact expected location of the TX tone
plt.axvline(x=100, color='red', linestyle='--', label='Expected TX Tone (100 kHz)')
plt.legend()
plt.show()
