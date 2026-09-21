# PersonaX async LLM benchmark

- Mode: `ollama:qwen2.5:3b`
- Tasks: 4
- Semaphore limit: 2
- Observed peak concurrency: 2

| Execution | Elapsed (ms) | Throughput (req/s) | Failed |
|---|---:|---:|---:|
| Serial baseline | 2277.0 | 1.757 | 0 |
| Async bounded | 2279.6 | 1.755 | 0 |

**Speedup: 0.999x; saved -2.6 ms.**

> A local Ollama server may serialize GPU work. Async improves waiting overlap and throughput only when the backend accepts concurrent work; it does not reduce one request's model inference time.
