// Accepted: prefix sums answer each query in O(1) -> O(n + q) overall.
#include <cstdio>
#include <vector>

int main() {
    int n, q;
    if (scanf("%d %d", &n, &q) != 2) return 0;
    std::vector<long long> prefix(n + 1, 0);
    for (int i = 1; i <= n; ++i) {
        long long x;
        scanf("%lld", &x);
        prefix[i] = prefix[i - 1] + x;
    }
    while (q--) {
        int l, r;
        scanf("%d %d", &l, &r);
        printf("%lld\n", prefix[r] - prefix[l - 1]);
    }
    return 0;
}
