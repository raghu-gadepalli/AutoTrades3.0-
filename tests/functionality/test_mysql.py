#!/usr/bin/env python3
import socket
import time
import statistics

# 
# Configuration
# 
MYSQL_HOST = "88.222.212.231"
MYSQL_PORT = 3306
TRIALS     = 10
# 

def measure_tcp_latency(host: str, port: int, trials: int):
    latencies = []
    for i in range(trials):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        start = time.perf_counter()
        try:
            sock.connect((host, port))
            elapsed = (time.perf_counter() - start) * 1000  # to milliseconds
            latencies.append(elapsed)
            print(f"[{i+1}/{trials}] Connected in {elapsed:.2f}ms")
        except Exception as e:
            print(f"[{i+1}/{trials}] Connection failed: {e}")
        finally:
            sock.close()
        # short pause between trials
        time.sleep(0.2)

    return latencies

if __name__ == "__main__":
    print(f"Measuring TCP connect latency to {MYSQL_HOST}:{MYSQL_PORT} over {TRIALS} trials...\n")
    lats = measure_tcp_latency(MYSQL_HOST, MYSQL_PORT, TRIALS)
    if lats:
        print("\nResults:")
        print(f"  min   = {min(lats):.2f}ms")
        print(f"  avg   = {statistics.mean(lats):.2f}ms")
        print(f"  max   = {max(lats):.2f}ms")
        print(f"  stdev = {statistics.stdev(lats):.2f}ms")
    else:
        print("\nNo successful connections recorded.")
