// Correct O(n) solution: a running frequency map counts, for each new element,
// how many earlier elements complete a pair (handling duplicates correctly).
#include <cstdio>
#include <unordered_map>

int main() {
    int n;
    long long target;
    if (scanf("%d %lld", &n, &target) != 2) return 0;
    std::unordered_map<long long, long long> freq;
    long long count = 0;
    for (int i = 0; i < n; ++i) {
        long long x;
        scanf("%lld", &x);
        count += freq[target - x];  // every earlier matching element forms a pair
        freq[x]++;
    }
    printf("%lld\n", count);
    return 0;
}
