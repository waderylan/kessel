# Python vs C++ gateway benchmark

- Workload: sequential OpenAI-compatible requests through a fresh mock provider process
- Runs: 100
- Python mean: 27.816 ms
- C++ mean: 17.225 ms
- C++ mean latency reduction: 38.1%

Provider network latency is intentionally excluded by the deterministic mock provider.
