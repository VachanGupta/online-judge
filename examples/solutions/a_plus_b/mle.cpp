// Memory Limit Exceeded: allocate and TOUCH ~400 MiB (well past a 256 MiB
// limit). Touching every page forces the resident set up so the kernel OOM
// killer fires, rather than the allocation staying lazy/virtual.
#include <cstddef>
#include <vector>

int main() {
    const std::size_t n = 400ULL * 1024 * 1024;
    std::vector<char> big(n, 1);  // constructor fills => pages are resident
    volatile char sink = 0;
    for (std::size_t i = 0; i < big.size(); i += 4096) sink += big[i];
    return sink != 0 ? 0 : 0;
}
