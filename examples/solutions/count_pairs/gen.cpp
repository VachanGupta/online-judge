// Test generator for stress mode. Reads "seed size" on stdin and prints a valid
// count-pairs instance: n = size values in 1..5 (so duplicates are common) with
// a fixed target of 6. Deterministic for a fixed (seed, size) — it seeds a PRNG
// and uses no other source of randomness — which is required for the shrinker to
// replay a failing seed.
#include <cstdio>
#include <random>

int main() {
    long long seed = 0, size = 0;
    if (scanf("%lld %lld", &seed, &size) != 2) return 0;
    if (size < 1) size = 1;

    std::mt19937_64 rng(static_cast<unsigned long long>(seed));
    int n = static_cast<int>(size);
    int target = 6;
    printf("%d %d\n", n, target);
    for (int i = 0; i < n; ++i) {
        int value = static_cast<int>(rng() % 5) + 1;  // 1..5
        printf("%d%c", value, (i + 1 < n) ? ' ' : '\n');
    }
    return 0;
}
