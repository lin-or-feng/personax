# PersonaX async LLM benchmark

- Mode: `simulated_io`
- Tasks: 8
- Semaphore limit: 2
- Observed peak concurrency: 2

| Execution | Elapsed (ms) | Throughput (req/s) | Failed |
|---|---:|---:|---:|
| Serial baseline | 2004.1 | 3.992 | 0 |
| Async bounded | 1000.8 | 7.994 | 0 |

**Speedup: 2.002x; saved 1003.3 ms.**

> A local Ollama server may serialize GPU work. Async improves waiting overlap and throughput only when the backend accepts concurrent work; it does not reduce one request's model inference time.
