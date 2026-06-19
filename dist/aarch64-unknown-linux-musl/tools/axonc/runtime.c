/* AXON native runtime.
 *
 * Minimal, dependency-free C11 support library linked into every program the
 * AXON C backend emits. Provides the `str` ABI type, value formatting, and the
 * core I/O / panic intrinsics. Compiled together with the generated program by
 * the host C compiler, so the resulting executable is genuine machine code with
 * no interpreter dependency.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <math.h>

typedef struct {
    const char *data;
    int64_t len;
} AxStr;

static AxStr ax_str_lit(const char *s) {
    AxStr r;
    r.data = s;
    r.len = (int64_t)strlen(s);
    return r;
}

static AxStr ax_str_take(char *s) {
    AxStr r;
    r.data = s;
    r.len = (int64_t)strlen(s);
    return r;
}

static AxStr ax_fmt_int(int64_t v) {
    char *b = (char *)malloc(24);
    snprintf(b, 24, "%lld", (long long)v);
    return ax_str_take(b);
}

static AxStr ax_fmt_uint(uint64_t v) {
    char *b = (char *)malloc(24);
    snprintf(b, 24, "%llu", (unsigned long long)v);
    return ax_str_take(b);
}

static AxStr ax_fmt_float(double v) {
    char *b = (char *)malloc(40);
    /* shortest round-tripping representation, matching the interpreter */
    for (int p = 1; p <= 17; p++) {
        snprintf(b, 40, "%.*g", p, v);
        if (strtod(b, NULL) == v) break;
    }
    if (!strpbrk(b, ".eEnN")) {  /* integral float -> append .0 (not for inf/nan) */
        size_t n = strlen(b);
        b[n] = '.';
        b[n + 1] = '0';
        b[n + 2] = '\0';
    }
    return ax_str_take(b);
}

static AxStr ax_fmt_bool(bool v) {
    return ax_str_lit(v ? "true" : "false");
}

static AxStr ax_str_concat(AxStr a, AxStr b) {
    char *buf = (char *)malloc((size_t)(a.len + b.len + 1));
    memcpy(buf, a.data, (size_t)a.len);
    memcpy(buf + a.len, b.data, (size_t)b.len);
    buf[a.len + b.len] = '\0';
    return ax_str_take(buf);
}

static bool ax_str_eq(AxStr a, AxStr b) {
    return a.len == b.len && memcmp(a.data, b.data, (size_t)a.len) == 0;
}

static int64_t ax_str_len(AxStr s) {
    return s.len;
}

static int ax_str_cmp(AxStr a, AxStr b) {
    int64_t n = a.len < b.len ? a.len : b.len;
    int c = memcmp(a.data, b.data, (size_t)n);
    if (c != 0) return c < 0 ? -1 : 1;
    if (a.len == b.len) return 0;
    return a.len < b.len ? -1 : 1;
}

static void ax_print(AxStr s) {
    fwrite(s.data, 1, (size_t)s.len, stdout);
}

static void ax_println(AxStr s) {
    fwrite(s.data, 1, (size_t)s.len, stdout);
    fputc('\n', stdout);
}

static void ax_eprint(AxStr s) {
    fwrite(s.data, 1, (size_t)s.len, stderr);
}

static void ax_eprintln(AxStr s) {
    fwrite(s.data, 1, (size_t)s.len, stderr);
    fputc('\n', stderr);
}

static void ax_panic(AxStr msg) {
    fputs("axon: panic: ", stderr);
    fwrite(msg.data, 1, (size_t)msg.len, stderr);
    fputc('\n', stderr);
    exit(101);
}

static void ax_assert(bool cond, AxStr msg) {
    if (!cond) ax_panic(msg);
}

static int64_t ax_idiv(int64_t a, int64_t b) {
    if (b == 0) ax_panic(ax_str_lit("integer division by zero"));
    return a / b; /* C truncates toward zero, matching AXON */
}

static int64_t ax_imod(int64_t a, int64_t b) {
    if (b == 0) ax_panic(ax_str_lit("modulo by zero"));
    return a % b;
}
