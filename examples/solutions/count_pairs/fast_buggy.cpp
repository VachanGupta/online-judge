// Plausible-looking but WRONG O(n) solution: it uses a *set* of seen values
// instead of a frequency map, so when the same complement value appears more
// than once it is counted at most once. It undercounts whenever duplicates
// matter (e.g. several 3's with target 6). Stress mode finds a counterexample
// for it, while fast.cpp passes — exactly the kind of subtle bug this catches.
#include <cstdio>
#include <set>

int main() {
    int n;
    long long target;
    if (scanf("%d %lld", &n, &target) != 2) return 0;
    std::set<long long> seen;
    long long count = 0;
    for (int i = 0; i < n; ++i) {
        long long x;
        scanf("%lld", &x);
        if (seen.count(target - x)) ++count;  // BUG: ignores how MANY earlier matches
        seen.insert(x);
    }
    printf("%lld\n", count);
    return 0;
}
