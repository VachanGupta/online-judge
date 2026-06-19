// Brute force oracle: O(n^2) — check every unordered pair. Trusted reference.
#include <cstdio>
#include <vector>

int main() {
    int n;
    long long target;
    if (scanf("%d %lld", &n, &target) != 2) return 0;
    std::vector<long long> a(n);
    for (auto& x : a) scanf("%lld", &x);

    long long count = 0;
    for (int i = 0; i < n; ++i)
        for (int j = i + 1; j < n; ++j)
            if (a[i] + a[j] == target) ++count;
    printf("%lld\n", count);
    return 0;
}
