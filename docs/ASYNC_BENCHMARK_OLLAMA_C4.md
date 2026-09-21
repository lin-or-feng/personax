# PersonaX async LLM benchmark

- Mode: `ollama:qwen2.5:3b`
- Tasks: 4
- Semaphore limit: 4
- Observed peak concurrency: 4

| Execution | Elapsed (ms) | Throughput (req/s) | Failed |
|---|---:|---:|---:|
| Serial baseline | 2277.1 | 1.757 | 0 |
| Async bounded | 2284.8 | 1.751 | 0 |

**Speedup: 0.997x; saved -7.7 ms.**

> A local Ollama server may serialize GPU work. Async improves waiting overlap and throughput only when the backend accepts concurrent work; it does not reduce one request's model inference time.
