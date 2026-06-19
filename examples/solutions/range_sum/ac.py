# Accepted: prefix sums, O(n + q). Uses sys.stdin for fast I/O.
import sys


def main() -> None:
    data = sys.stdin.buffer.read().split()
    idx = 0
    n = int(data[idx]); idx += 1
    q = int(data[idx]); idx += 1

    prefix = [0] * (n + 1)
    for i in range(1, n + 1):
        prefix[i] = prefix[i - 1] + int(data[idx]); idx += 1

    out = []
    for _ in range(q):
        left = int(data[idx]); idx += 1
        right = int(data[idx]); idx += 1
        out.append(str(prefix[right] - prefix[left - 1]))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))


main()
