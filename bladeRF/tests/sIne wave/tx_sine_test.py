import SoapySDR
from SoapySDR import SOAPY_SDR_TX, SOAPY_SDR_CF32
import numpy as np
import sys

# 1. Configuration Parameters
freq = 2450e6      # Center frequency: 2.45 GHz (ISM Band)
rate = 1e6         # Sample rate: 1 MSps
tx_gain = 30       # TX Gain in dB (Range is -23.75 to 66 dB)
tone_freq = 100e3  # Tone offset: 100 kHz (Transmits at 2450.1 MHz)
buffer_size = 4096 # Must match block size in SoapySDR probe

# 2. Hardware Initialization
print("Initializing bladeRF...")
args = dict(driver="bladerf")
try:
    sdr = SoapySDR.Device(args)
except Exception as e:
    print(f"Error initializing device: {e}")
    sys.exit(1)

# 3. Channel Setup (TX Channel 0)
sdr.setSampleRate(SOAPY_SDR_TX, 0, rate)
sdr.setFrequency(SOAPY_SDR_TX, 0, freq)
sdr.setGain(SOAPY_SDR_TX, 0, tx_gain)
sdr.setAntenna(SOAPY_SDR_TX, 0, "TX")

tx_stream = sdr.setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32)
sdr.activateStream(tx_stream)

# 4. Signal Generation (Complex Exponential for Single Sideband)
# Using np.complex64 to match the SOAPY_SDR_CF32 stream format
t = np.arange(buffer_size) / rate
iq_signal = np.exp(1j * 2 * np.pi * tone_freq * t).astype(np.complex64)

print(f"Hardware configured.")
print(f"Center Freq: {freq / 1e6} MHz")
print(f"Tone Offset: {tone_freq / 1e3} kHz")
print(f"Output Freq: {(freq + tone_freq) / 1e6} MHz")
print("Transmitting... Press Ctrl+C to stop.")

# 5. Synchronous Transmission Loop
try:
    while True:
        # writeStream is blocking and will pace the loop to the sample rate
        status = sdr.writeStream(tx_stream, [iq_signal], buffer_size)
        if status.ret != buffer_size:
            print(f"Buffer write error/underflow. Status: {status.ret}")
except KeyboardInterrupt:
    print("\nStopping transmission gracefully.")
finally:
    # 6. Teardown
    sdr.deactivateStream(tx_stream)
    sdr.closeStream(tx_stream)
