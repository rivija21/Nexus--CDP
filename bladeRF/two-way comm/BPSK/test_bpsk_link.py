import os, random, sys, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpsk_link as L

random.seed(7)
import shutil
for d in ('/tmp/bpsk_rx_a', '/tmp/bpsk_rx_b'):
    shutil.rmtree(d, ignore_errors=True); os.makedirs(d)

A = L.LinkState(1, 2, ack_timeout=0.2, max_retries=8, frag_size=64, rx_dir='/tmp/bpsk_rx_a')
B = L.LinkState(2, 1, ack_timeout=0.2, max_retries=8, frag_size=64, rx_dir='/tmp/bpsk_rx_b')

# make a test file
payload = bytes(range(256)) * 9 + b'tail'
open('/tmp/bpsk_testfile.bin','wb').write(payload)

air = []          # (deliver_time, dest, frame)
logs = {1: [], 2: []}
now = 0.0
LOSS = 0.15
PROP = 0.02

def run(node, actions, me):
    for kind, val in actions:
        if kind == 'tx':
            if random.random() > LOSS:
                air.append((now + PROP, 2 if me == 1 else 1, val))
        else:
            logs[me].append(val)

run(A, A.on_user('/sendfile /tmp/bpsk_testfile.bin', now), 1)
run(B, B.on_user('hello from node B', now), 2)

for step in range(200000):
    now = round(now + 0.001, 6)
    due = [x for x in air if x[0] <= now]
    air[:] = [x for x in air if x[0] > now]
    for _, dest, frame in due:
        node = A if dest == 1 else B
        run(node, node.on_rx(frame, now), dest)
    if step % 10 == 0:
        run(A, A.on_tick(now), 1)
        run(B, B.on_tick(now), 2)
    if os.path.exists('/tmp/bpsk_rx_b/rx_bpsk_testfile.bin') and any('hello' in l for l in logs[1]):
        break

print('sim time %.2f s' % now)
print('--- node1 log ---'); [print(' ', l) for l in logs[1][:12]]
print('--- node2 log ---'); [print(' ', l) for l in logs[2][-6:]]
print('A:', A.stats_line()); print('B:', B.stats_line())
got = open('/tmp/bpsk_rx_b/rx_bpsk_testfile.bin','rb').read()
print('file transferred intact:', got == payload, len(got), 'bytes')
