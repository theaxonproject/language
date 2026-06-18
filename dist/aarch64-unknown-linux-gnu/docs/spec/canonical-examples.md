# Canonical AXON Examples

The `examples/` directory contains the ten required canonical programs:

1. `01_hello.ax` - Hello, World.
2. `02_fizzbuzz.ax` - Looping, conditionals, and string formatting.
3. `03_fibonacci.ax` - Recursive and iterative functions.
4. `04_http_get.ax` - HTTP client usage.
5. `05_json_file.ax` - File I/O and JSON parsing.
6. `06_concurrent_tasks.ax` - Tasks, task groups, and channels.
7. `07_cli_tool.ax` - CLI argument parsing and file I/O.
8. `08_tcp_echo_server.ax` - TCP listener and per-connection tasks.
9. `09_struct_trait.ax` - Structs, methods, traits, and impl blocks.
10. `10_error_chain.ax` - Result propagation and inline error handling.

These examples are specification tests. Once the Stage 0 parser exists, every
example must parse successfully. Once the compiler reaches Phase 12, every
example must compile and run on all supported targets.
