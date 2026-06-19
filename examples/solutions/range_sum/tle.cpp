// Time Limit Exceeded: recompute each range sum by iterating l..r.
// This is O(n * q); on the large test (n = q = 2e5 with full-width ranges)
// that is ~4e10 operations — far beyond the time limit — while the prefix-sum
// solution in ac.cpp answers the same input in milliseconds. The two differ
// only in algorithmic complexity, so this is a clean demonstration that the
// time limit (not language speed) is doing the work.
#include <cstdio>
#include <vector>

int main() {
    int n, q;
    if (scanf("%d %d", &n, &q) != 2) return 0;
    std::vector<long long> a(n + 1, 0);
    for (int i = 1; i <= n; ++i) scanf("%lld", &a[i]);
    while (q--) {
        int l, r;
        scanf("%d %d", &l, &r);
        long long sum = 0;
        for (int i = l; i <= r; ++i) sum += a[i];
        printf("%lld\n", sum);
    }
    return 0;
}
