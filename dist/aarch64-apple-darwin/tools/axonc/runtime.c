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

static int64_t ax_ipow(int64_t base, int64_t exp) {
    int64_t out = 1;
    if (exp < 0) return 0;
    while (exp > 0) {
        if (exp & 1) out *= base;
        base *= base;
        exp >>= 1;
    }
    return out;
}

static void *ax_alloc(size_t n) {
    void *p = malloc(n ? n : 1);
    if (p == NULL) ax_panic(ax_str_lit("out of memory"));
    return p;
}

static void *ax_realloc(void *old, size_t n) {
    void *p = realloc(old, n ? n : 1);
    if (p == NULL) ax_panic(ax_str_lit("out of memory"));
    return p;
}

/* ---- string library ---- */

static AxStr ax_str_from(const char *data, int64_t len) {
    char *buf = (char *)ax_alloc((size_t)len + 1);
    memcpy(buf, data, (size_t)len);
    buf[len] = '\0';
    AxStr r;
    r.data = buf;
    r.len = len;
    return r;
}

static bool ax_str_is_empty(AxStr s) {
    return s.len == 0;
}

static bool ax_str_contains(AxStr s, AxStr needle) {
    if (needle.len == 0) return true;
    if (needle.len > s.len) return false;
    for (int64_t i = 0; i + needle.len <= s.len; i++) {
        if (memcmp(s.data + i, needle.data, (size_t)needle.len) == 0) return true;
    }
    return false;
}

/* Blocking console input. Blank lines are values; EOF is absence. */
static bool ax_read_line(AxStr *out) {
    fflush(stdout);
    size_t len = 0, cap = 128;
    char *buf = (char *)ax_alloc(cap);
    int ch;
    while ((ch = fgetc(stdin)) != EOF && ch != '\n') {
        if (len + 1 >= cap) {
            cap *= 2;
            buf = (char *)ax_realloc(buf, cap);
        }
        buf[len++] = (char)ch;
    }
    if (ferror(stdin)) {
        free(buf);
        ax_panic(ax_str_lit("cannot read stdin"));
    }
    if (ch == EOF && len == 0) {
        free(buf);
        return false;
    }
    if (ch == '\n' && len && buf[len - 1] == '\r') len--;
    buf[len] = '\0';
    out->data = buf;
    out->len = (int64_t)len;
    return true;
}

static bool ax_str_starts_with(AxStr s, AxStr prefix) {
    return prefix.len <= s.len &&
           memcmp(s.data, prefix.data, (size_t)prefix.len) == 0;
}

static bool ax_str_ends_with(AxStr s, AxStr suffix) {
    return suffix.len <= s.len &&
           memcmp(s.data + (s.len - suffix.len), suffix.data, (size_t)suffix.len) == 0;
}

static bool ax_is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

static AxStr ax_str_trim_start(AxStr s) {
    int64_t i = 0;
    while (i < s.len && ax_is_space(s.data[i])) i++;
    return ax_str_from(s.data + i, s.len - i);
}

static AxStr ax_str_trim_end(AxStr s) {
    int64_t n = s.len;
    while (n > 0 && ax_is_space(s.data[n - 1])) n--;
    return ax_str_from(s.data, n);
}

static AxStr ax_str_trim(AxStr s) {
    int64_t i = 0, n = s.len;
    while (i < n && ax_is_space(s.data[i])) i++;
    while (n > i && ax_is_space(s.data[n - 1])) n--;
    return ax_str_from(s.data + i, n - i);
}

static AxStr ax_str_to_upper(AxStr s) {
    char *buf = (char *)ax_alloc((size_t)s.len + 1);
    for (int64_t i = 0; i < s.len; i++) {
        char c = s.data[i];
        buf[i] = (c >= 'a' && c <= 'z') ? (char)(c - 32) : c;
    }
    buf[s.len] = '\0';
    AxStr r; r.data = buf; r.len = s.len; return r;
}

static AxStr ax_str_to_lower(AxStr s) {
    char *buf = (char *)ax_alloc((size_t)s.len + 1);
    for (int64_t i = 0; i < s.len; i++) {
        char c = s.data[i];
        buf[i] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
    }
    buf[s.len] = '\0';
    AxStr r; r.data = buf; r.len = s.len; return r;
}

static AxStr ax_str_replace(AxStr s, AxStr from, AxStr to) {
    if (from.len == 0) return s;
    /* count occurrences */
    int64_t count = 0;
    for (int64_t i = 0; i + from.len <= s.len;) {
        if (memcmp(s.data + i, from.data, (size_t)from.len) == 0) { count++; i += from.len; }
        else i++;
    }
    int64_t out_len = s.len + count * (to.len - from.len);
    char *buf = (char *)ax_alloc((size_t)out_len + 1);
    int64_t o = 0;
    for (int64_t i = 0; i < s.len;) {
        if (i + from.len <= s.len && memcmp(s.data + i, from.data, (size_t)from.len) == 0) {
            memcpy(buf + o, to.data, (size_t)to.len);
            o += to.len; i += from.len;
        } else {
            buf[o++] = s.data[i++];
        }
    }
    buf[out_len] = '\0';
    AxStr r; r.data = buf; r.len = out_len; return r;
}

/* Python-style clamped slice [start, end) with negative-index support. */
static AxStr ax_str_slice(AxStr s, int64_t start, int64_t end) {
    if (start < 0) start += s.len;
    if (end < 0) end += s.len;
    if (start < 0) start = 0;
    if (end > s.len) end = s.len;
    if (start >= end) return ax_str_lit("");
    return ax_str_from(s.data + start, end - start);
}

static AxStr ax_str_index(AxStr s, int64_t i) {
    if (i < 0) i += s.len;
    if (i < 0 || i >= s.len) ax_panic(ax_str_lit("string index out of range"));
    return ax_str_from(s.data + i, 1);
}

static AxStr ax_str_repeat(AxStr s, int64_t n) {
    if (n <= 0) return ax_str_lit("");
    char *buf = (char *)ax_alloc((size_t)(s.len * n) + 1);
    for (int64_t i = 0; i < n; i++) memcpy(buf + i * s.len, s.data, (size_t)s.len);
    buf[s.len * n] = '\0';
    AxStr r; r.data = buf; r.len = s.len * n; return r;
}

static bool ax_str_parse_int(AxStr s, int64_t *out) {
    AxStr t = ax_str_trim(s);
    if (t.len == 0) return false;
    char *endp = NULL;
    long long v = strtoll(t.data, &endp, 10);
    if (endp == NULL || endp != t.data + t.len) return false;
    *out = (int64_t)v;
    return true;
}

static bool ax_str_parse_float(AxStr s, double *out) {
    AxStr t = ax_str_trim(s);
    if (t.len == 0) return false;
    char *endp = NULL;
    double v = strtod(t.data, &endp);
    if (endp == NULL || endp != t.data + t.len) return false;
    *out = v;
    return true;
}

/* quoted display form used when strings are shown inside containers */
static AxStr ax_show_str_quoted(AxStr s) {
    return s;
}

/* ---- file I/O ---- */

static bool ax_file_exists(AxStr path) {
    FILE *fh = fopen(path.data, "rb");
    if (fh == NULL) return false;
    fclose(fh);
    return true;
}

static AxStr ax_file_open(AxStr path) {
    if (!ax_file_exists(path)) {
        ax_panic(ax_str_concat(ax_str_lit("file not found: "), path));
    }
    return path;
}

static bool ax_read_file(AxStr path, AxStr *out) {
    FILE *fh = fopen(path.data, "rb");
    if (fh == NULL) return false;
    if (fseek(fh, 0, SEEK_END) != 0) { fclose(fh); return false; }
    long size = ftell(fh);
    if (size < 0) { fclose(fh); return false; }
    rewind(fh);
    char *buf = (char *)malloc((size_t)size + 1);
    if (buf == NULL) { fclose(fh); return false; }
    size_t got = fread(buf, 1, (size_t)size, fh);
    fclose(fh);
    buf[got] = '\0';
    out->data = buf;
    out->len = (int64_t)got;
    return true;
}

static AxStr ax_file_read_all(AxStr path) {
    AxStr out;
    if (!ax_read_file(path, &out)) {
        ax_panic(ax_str_concat(ax_str_lit("cannot read file: "), path));
    }
    return out;
}

static bool ax_write_file(AxStr path, AxStr data) {
    FILE *fh = fopen(path.data, "wb");
    if (fh == NULL) return false;
    size_t wrote = fwrite(data.data, 1, (size_t)data.len, fh);
    fclose(fh);
    return wrote == (size_t)data.len;
}

static int64_t ax_iabs(int64_t v) { return v < 0 ? -v : v; }
static int64_t ax_imax(int64_t a, int64_t b) { return a > b ? a : b; }
static int64_t ax_imin(int64_t a, int64_t b) { return a < b ? a : b; }
static int64_t ax_iclamp(int64_t v, int64_t lo, int64_t hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
static double ax_fmax_(double a, double b) { return a > b ? a : b; }
static double ax_fmin_(double a, double b) { return a < b ? a : b; }
static int64_t ax_fround(double v) { return (int64_t)llround(v); }
