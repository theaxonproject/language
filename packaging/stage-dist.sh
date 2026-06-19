#!/usr/bin/env bash
set -euo pipefail

raw_version="${1:?version is required}"
target="${2:?target triple is required}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="${raw_version#v}"
if [[ ! "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$ ]]; then
    echo "Invalid AXON version '$raw_version'. Expected semantic version such as 1.0.0." >&2
    exit 1
fi

case "$target" in
    x86_64-apple-darwin|aarch64-apple-darwin|x86_64-unknown-linux-gnu|x86_64-unknown-linux-musl|aarch64-unknown-linux-gnu|aarch64-unknown-linux-musl)
        ;;
    *)
        echo "Unsupported Unix staging target: $target" >&2
        exit 1
        ;;
esac

dist_root="$repo_root/dist/$target"
bin_dir="$dist_root/bin"
tool_impl_dir="$dist_root/tools"
stdlib_dir="$dist_root/stdlib"
tools=(axc axbuild axpkg axls axfmt axrepl axlint axup)

rm -rf "$dist_root"
mkdir -p "$bin_dir" "$tool_impl_dir" "$stdlib_dir"

cp "$repo_root/tools/axon_tool.py" "$tool_impl_dir/axon_tool.py"
chmod 0644 "$tool_impl_dir/axon_tool.py"

# Copy the compiler package (lexer, parser, checker, interpreter, C backend).
cp -R "$repo_root/tools/axonc" "$tool_impl_dir/axonc"
find "$tool_impl_dir/axonc" -name '__pycache__' -type d -prune -exec rm -rf {} +
chmod -R 0644 "$tool_impl_dir/axonc"/*.py "$tool_impl_dir/axonc"/*.c 2>/dev/null || true

for tool in "${tools[@]}"; do
    cat > "$bin_dir/$tool" <<SCRIPT
#!/usr/bin/env sh
set -eu
root=\$(CDPATH= cd -- "\$(dirname -- "\$0")/.." && pwd)
exec python3 "\$root/tools/axon_tool.py" "$tool" "\$@"
SCRIPT
    chmod 0755 "$bin_dir/$tool"
done

cat > "$stdlib_dir/README.md" <<EOF
# AXON Standard Library

This distribution includes the AXON standard-library contract and the reference
toolchain runtime required by the canonical examples.
EOF

cp "$repo_root/LICENSE" "$dist_root/LICENSE"
cp "$repo_root/README.md" "$dist_root/README.md"
cp -R "$repo_root/docs" "$dist_root/docs"
cp -R "$repo_root/examples" "$dist_root/examples"

echo "Staged AXON distribution: $dist_root"
