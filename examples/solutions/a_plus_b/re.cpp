// Runtime Error: null pointer dereference -> SIGSEGV.
#include <iostream>

int main() {
    long long a, b;
    std::cin >> a >> b;
    int* p = nullptr;
    return *p;  // segfault
}
