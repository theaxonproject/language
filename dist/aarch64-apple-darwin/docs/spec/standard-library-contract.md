# AXON Standard Library Contract

The standard library is named `axstd` and is imported through top-level module
aliases such as `Core`, `Io`, `Http`, and `Json`.

Every public symbol must have doc comments, examples, error conditions, and
cross-links in generated documentation. APIs that allocate, block, or perform
I/O must say so explicitly.

## Module Set

AXON 1.0 includes:

- `axstd.core`
- `axstd.str`
- `axstd.collections`
- `axstd.io`
- `axstd.net`
- `axstd.http`
- `axstd.json`
- `axstd.watch`
- `axstd.process`
- `axstd.time`
- `axstd.math`
- `axstd.crypto`
- `axstd.regex`
- `axstd.cli`
- `axstd.log`
- `axstd.test`
- `axstd.serde`
- `axstd.proptest`

## Error Policy

Stdlib fallibility is represented with `T!E`. Panics are reserved for violated
preconditions that would indicate a programming error, such as indexing through
an explicitly unchecked API with an invalid index in debug builds.

## Naming Policy

Types use `PascalCase`, functions and methods use `snake_case`, and constants
use `SCREAMING_SNAKE_CASE`.

## Core Required Surface

```axon
fn panic(msg: str) -> never
fn assert(cond: bool, msg: str)
fn assert_eq(T)(a: T, b: T) where T: Eq + Debug
fn print(args: ...any)
fn println(args: ...any)
fn eprint(args: ...any)
fn eprintln(args: ...any)
fn read_line() -> Option(str)
fn type_name(T)() -> str
fn size_of(T)() -> uint
fn todo(msg: str) -> never
fn unreachable() -> never
fn unimplemented() -> never
```

`read_line()` flushes stdout, then blocks for one line from standard input.
It allocates a string and returns `Some(text)` without the LF or CRLF line
ending. A blank line is `Some("")`; EOF before any characters is `None`.
An unterminated final line is returned before EOF. I/O failure panics.
Both native programs and the interpreter implement this function.

```axon
print("Name: ")
if let Some(name) = read_line() then
    println(f"Hello, {name}")
```

## Collection Required Types

The collections module must provide:

- `Vec(T)`
- `Map(K, V)`
- `OrderedMap(K, V)`
- `Set(T)`
- `OrderedSet(T)`
- `Deque(T)`
- `Heap(T)`
- `LinkedList(T)`

`Vec(T)` must include `push`, `pop`, `insert`, `remove`, `get`, `get_mut`,
`len`, `is_empty`, `clear`, `contains`, `iter`, `map`, `filter`, `fold`, `sort`,
`sort_by`, `reverse`, `dedup`, `extend`, `slice`, `first`, `last`, `windows`,
and `chunks`.

## I/O Required Types

The I/O module must provide `File`, `Dir`, `Path`, `Stdin`, `Stdout`, `Stderr`,
`BufReader(T)`, `BufWriter(T)`, `TempDir`, `TempFile`, and the traits `Reader`,
`Writer`, `Seeker`, and `Closer`.

## Networking and HTTP

Networking APIs are async under the task scheduler while exposing direct-looking
AXON calls. Blocking the OS thread is forbidden in scheduler-managed tasks.

HTTP includes `HttpClient`, `Request`, `Response`, `Headers`, `StatusCode`,
`HttpServer`, `Router`, and middleware. TLS is part of the standard
distribution.

## Serialization

`axstd.serde` defines `Serialize` and `Deserialize` traits. JSON, TOML, YAML,
CSV, and MessagePack are required formats. `@derive(Serialize, Deserialize)` and
`@derive(Json)` generate implementations during semantic analysis.

## Testing

`axstd.test` provides `@test`, `@bench`, `expect`, `@skip`, `@only`, and
`@timeout`. Examples in documentation are compiled and run as tests.

`axstd.proptest` provides `@property_test`, shrinking, custom generators through
`Generate`, and stateful model testing.
