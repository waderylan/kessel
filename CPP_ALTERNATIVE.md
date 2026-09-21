# C++ alternative

`kessel-cpp/` is a separate C++20 implementation of Kessel. It was built to
test whether a native gateway could preserve the Python service's commands and
API contracts while reducing process and HTTP overhead.

The implementation is intentionally isolated from the Python package. It has
its own build, tests, web assets, provider adapters, benchmark harness, and
documentation. The Python source was not changed while this alternative was
developed.

The measured deterministic gateway benchmark showed a 38.1% reduction in mean
request latency for the C++ implementation. This measures gateway and provider
process overhead; it does not imply faster model inference.

The final project does not need to include or distribute both implementations.
`kessel-cpp/` is a potential replacement or reference implementation that can
be evaluated independently. Keep the Python service for the established stack,
or select the C++ directory when native deployment and lower gateway overhead
justify the additional C++ build dependencies.

Build, compatibility, verification, and benchmark details are in
`kessel-cpp/README.md`.
